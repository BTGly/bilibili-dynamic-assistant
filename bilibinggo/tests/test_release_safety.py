from __future__ import annotations
import copy
import json
from pathlib import Path
import sqlite3
import sys
import zipfile
import pytest
from src.activity_store import replace_all_activities, update_activity
from src.activity_cleanup import preview_cleanup, apply_cleanup
from src.comment_preview import create_preview, validate_preview, confirm_comment_review, ensure_auto_comment_review
from src.db.engine import db_path
from src.dead_links import mark_dynamic_inaccessible, purge_dynamic_ids
from src.safe_export import build_shareable_bundle
from src.secure_files import permission_status, harden_file_permissions, write_text_secret
from src.test_isolation import assert_test_path

TARGET = '100000000000000001'


def activity():
    return {'dynamic_id': TARGET, 'lottery_type': '转发抽奖', 'lottery_time': 2000000000,
            'conditions': {'need_follow': True, 'need_repost': True, 'need_comment': True},
            'activity_status': '未参加', 'draw_status': 'active', 'prizes': [{'description': '键盘'}]}


def test_shareable_export_excludes_raw_secrets_in_db_backups_and_unknown_files(tmp_path):
    home = tmp_path / 'source'
    (home / 'config').mkdir(parents=True)
    (home / 'data/backups').mkdir(parents=True)
    secret = 'private-credential-example-value'
    (home / 'config/cookies.txt').write_text('SESSDATA=' + secret)
    (home / 'config/llm.env').write_text('LLM_API_KEY=' + secret)
    (home / 'data/backups/old.db').write_text(secret)
    (home / 'data/arbitrary.log').write_text(secret)
    with sqlite3.connect(home / 'data/binggo.db') as conn:
        conn.execute('CREATE TABLE jobs (payload_json TEXT, message TEXT)')
        conn.execute('INSERT INTO jobs VALUES (?,?)', (json.dumps({'nested': {'x_api_key': secret}}), secret))
    output = tmp_path / 'share.zip'
    result = build_shareable_bundle(home, output)
    assert result['database']['sensitive_cells'] == 2
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ['diagnostic-summary.json']
        content = archive.read('diagnostic-summary.json').decode()
        assert secret not in content
        assert json.loads(content)['database']['tables']['jobs'] == 1
    with pytest.raises(FileExistsError):
        build_shareable_bundle(home, output)


def test_private_backup_retains_credentials_and_consistent_database(tmp_path):
    from scripts.export_migrate_bundle import build_bundle
    home = tmp_path / 'source'
    (home / 'config').mkdir(parents=True)
    (home / 'data').mkdir()
    (home / 'config/cookies.txt').write_text('SESSDATA=private-test-value')
    with sqlite3.connect(home / 'data/binggo.db') as conn:
        conn.execute('CREATE TABLE example (value TEXT)')
        conn.execute("INSERT INTO example VALUES ('kept')")
    output = tmp_path / 'private.zip'
    report = build_bundle(source_home=home, appdata_home=None, out_zip=output, mode='private')
    assert not report['shareable']
    with zipfile.ZipFile(output) as archive:
        assert archive.read('config/cookies.txt') == b'SESSDATA=private-test-value'
        assert archive.read('data/binggo.db').startswith(b'SQLite format 3')


def test_missing_isolation_and_user_paths_abort_before_any_write(tmp_path, monkeypatch):
    outside = tmp_path.parent / 'should-not-be-created' / 'cookie.txt'
    with pytest.raises(RuntimeError, match='隔离'):
        write_text_secret(outside, 'dummy')
    assert not outside.exists()
    monkeypatch.delenv('BINGGO_TEST_DATA_ROOT')
    with pytest.raises(RuntimeError, match='未配置'):
        assert_test_path(tmp_path / 'data/binggo.db')
    monkeypatch.setenv('BINGGO_TEST_DATA_ROOT', str(Path.cwd()))
    with pytest.raises(RuntimeError, match='临时目录'):
        assert_test_path(Path.cwd() / 'data/binggo.db')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows ACL')
def test_windows_permissions_are_verified_and_report_failures(tmp_path, monkeypatch):
    path = tmp_path / 'credential.txt'
    path.write_text('dummy')
    assert harden_file_permissions(path)
    assert permission_status(path)['protected'] is True
    monkeypatch.setattr('src.secure_files._windows_acl', lambda *args, **kwargs: False)
    assert not harden_file_permissions(path)
    assert permission_status(path)['state'] == 'unprotected'
    assert permission_status(path)['last_result']['success'] is False


def test_inaccessible_activity_is_kept_until_explicit_backed_up_cleanup(tmp_path):
    replace_all_activities([activity()])
    mark_dynamic_inaccessible(TARGET, reason='详情当前不可访问')
    with sqlite3.connect(db_path()) as conn:
        before = conn.execute('SELECT payload_json FROM activities').fetchone()[0]
    preview = preview_cleanup(db_path())
    with sqlite3.connect(db_path()) as conn:
        assert conn.execute('SELECT payload_json FROM activities').fetchone()[0] == before
    with pytest.raises(RuntimeError, match='预览'):
        purge_dynamic_ids([TARGET])
    backup = tmp_path / 'before-cleanup.db'
    assert apply_cleanup(db_path(), preview, backup)['deleted'] == 1
    with sqlite3.connect(backup) as conn:
        assert conn.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == 1


def test_changed_cleanup_preview_never_deletes_any_target(tmp_path):
    replace_all_activities([activity()])
    mark_dynamic_inaccessible(TARGET, reason='暂不可访问')
    preview = preview_cleanup(db_path())
    with sqlite3.connect(db_path()) as conn:
        conn.execute('UPDATE activities SET updated_at=updated_at+1')
    with pytest.raises(RuntimeError, match='过期'):
        apply_cleanup(db_path(), preview, tmp_path / 'backup.db')
    with sqlite3.connect(db_path()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == 1


def test_optional_preview_binds_text_without_gating_automatic_execution():
    item = activity()
    replace_all_activities([item])
    preview = create_preview('participate', [TARGET], text='我的评论，符合已核查的原文')
    text = preview['body']['items'][0]['text']
    assert validate_preview(preview, action='participate', dynamic_ids=[TARGET]) == {TARGET: text}
    assert preview['body']['source'] == 'custom'
    ensure_auto_comment_review(TARGET, text)
    confirm_comment_review(TARGET, text)
    ensure_auto_comment_review(TARGET, text)
    ensure_auto_comment_review(TARGET, '修改后的评论')
    tampered = copy.deepcopy(preview)
    tampered['body']['items'][0]['text'] = '篡改后的评论'
    with pytest.raises(ValueError, match='无效'):
        validate_preview(tampered, action='participate', dynamic_ids=[TARGET])
    with pytest.raises(ValueError, match='目标'):
        validate_preview(preview, action='participate', dynamic_ids=[])
    item['conditions']['need_follow'] = False
    update_activity(TARGET, item)
    with pytest.raises(ValueError, match='规则'):
        validate_preview(preview, action='participate', dynamic_ids=[TARGET])


def test_explicit_action_text_never_borrows_others_comments(monkeypatch):
    """S2 已按产品决定重新启用 random_comment；唯一恒定：显式传入的 action_text 只用自己的文案，绝不读他人评论。"""
    from src.participate_text import resolve_participate_text_for_activity
    from src.user_settings import set_participate_text, set_participate_text_mode, get_participate_text_mode
    set_participate_text('自己写的文案')
    set_participate_text_mode('random_comment')
    assert get_participate_text_mode() == 'random_comment'
    monkeypatch.setattr('src.participate_text.fetch_reply_messages', lambda *args, **kwargs: pytest.fail('显式文案不应读取他人评论'))
    resolved = resolve_participate_text_for_activity(object(), dynamic_id=TARGET, explicit_text='自己写的文案')
    assert resolved.text == '自己写的文案'
    assert resolved.source == 'custom'


def test_api_participation_uses_saved_text_without_preview_or_llm(monkeypatch):
    from fastapi.testclient import TestClient
    from unittest.mock import MagicMock
    import web.app as module
    replace_all_activities([activity()])
    monkeypatch.setattr(module, 'get_account_profile', lambda: {'logged_in': True})
    monkeypatch.setattr(module, 'require_llm_ready', lambda: pytest.fail('参与不需要 LLM'))
    start = MagicMock(return_value=1)
    monkeypatch.setattr(module.runner, 'try_start', start)
    client = TestClient(module.app)
    request = {'action': 'participate', 'params': {'dynamic_id': TARGET}}
    assert client.post('/api/jobs', json=request).status_code == 200
    assert start.call_args.args[1] == {'dynamic_id': TARGET}
    request['params']['action_text'] = '自己的固定文案'
    assert client.post('/api/jobs', json=request).status_code == 200
    assert start.call_args.args[1]['action_text'] == '自己的固定文案'


def test_failed_detail_refresh_keeps_the_activity_and_review_state(monkeypatch):
    from src.fetch_activity_info import fetch_activity_info
    from src.activity_store import load_activities
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): return False
    item = activity()
    item['lottery_time'] = None
    replace_all_activities([item])
    monkeypatch.setattr('src.fetch_activity_info.BilibiliClient', Client)
    monkeypatch.setattr('src.fetch_activity_info.is_dynamic_deleted', lambda *args: True)
    result = fetch_activity_info(workers=1, continue_on_error=True)
    assert len(result.activities) == 1
    assert load_activities()[0]['accessibility']['state'] == 'inaccessible'


def test_auto_flow_without_explicit_text_defers_to_mode_resolver():
    """没传 action_text 时必须返回 None，让解析器按 participate_text_mode 决定；否则随机借用评论永远被自定义文案旁路。"""
    from web.actions import _reviewed_action_text
    assert _reviewed_action_text({}, TARGET, from_auto=True) is None
    assert _reviewed_action_text({"action_text": ""}, TARGET, from_auto=False) is None
    assert _reviewed_action_text({"action_text": "我的固定文案"}, TARGET, from_auto=True) == "我的固定文案"
