from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Literal

import httpx

from src.bilibili_auth import get_login_uid, require_login
from src.bilibili_client import BilibiliClient, api_code
from src.lottery_api import fetch_dynamic_detail, fetch_notice_for_interact, fetch_opus_detail_item
from src.sources.common import opus_link
from src.participation_guard import serial_writes, get_guard, set_guard, failure_kind, record_failure

ActionName = Literal["like", "follow", "favorite", "repost", "comment", "reserve"]

DEFAULT_PARTICIPATE_TEXT = "好运连连！"

LIKE_URL = "https://api.bilibili.com/x/dynamic/feed/dyn/thumb"
FOLLOW_URL = "https://api.bilibili.com/x/relation/modify"
RELATION_URL = "https://api.bilibili.com/x/relation"
USER_CARD_URL = "https://api.bilibili.com/x/web-interface/card"
FAV_LIST_URL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
FAV_RESOURCE_LIST_URL = "https://api.bilibili.com/x/v3/fav/resource/list"
FAV_DEAL_URL = "https://api.bilibili.com/x/v3/fav/resource/deal"
COSMO_SIMPLE_ACTION_URL = "https://api.bilibili.com/x/community/cosmo/interface/simple_action"
REPOST_URL = "https://api.vc.bilibili.com/dynamic_repost/v1/dynamic_repost/repost"
COMMENT_URL = "https://api.bilibili.com/x/v2/reply/add"
REPLY_MAIN_URL = "https://api.bilibili.com/x/v2/reply/main"

FAV_CONTENT_TYPE = 24
FOLLOWING_ATTRIBUTES = {2, 6}
SPACE_FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
SPACE_FEED_PAGE_SIZE = 20
SPACE_FEED_MAX_PAGES = 3
ACTION_INTERVAL_SEC = 1.5
ACTION_LABELS: dict[ActionName, str] = {
    "like": "点赞",
    "follow": "关注",
    "favorite": "收藏",
    "repost": "转发",
    "comment": "评论",
    "reserve": "预约",
}
PARTICIPATION_STEPS: tuple[ActionName, ...] = ("like", "follow", "favorite", "repost", "comment")


@dataclass
class ActionResult:
    action: ActionName
    ok: bool
    detail: str = ""
    state: str = ""
    required: bool | None = None


@dataclass
class DynamicContext:
    dynamic_id: str
    sender_uid: int
    referer: str
    comment_rid: str
    comment_type: int
    liked: bool
    favorited: bool
    favorite_available: bool
    followed: bool
    reposted: bool
    commented: bool
    chain_ids: tuple[str, ...] = ()


def _api_code(payload: dict) -> int:
    code = payload.get("code")
    if code is None:
        return -1
    try:
        return int(code)
    except (TypeError, ValueError):
        return -1


def _api_message(payload: dict) -> str:
    return str(payload.get("message") or payload.get("msg") or "").strip()


def _default_fav_folder_id(client: BilibiliClient, *, uid: int, referer: str) -> str:
    payload = client.get_json(FAV_LIST_URL, params={"up_mid": uid}, referer=referer)
    folders = (payload.get("data") or {}).get("list") or []
    if not folders:
        raise RuntimeError("未找到可用收藏夹")
    folder = folders[0]
    folder_id = folder.get("id") or folder.get("fid")
    if not folder_id:
        raise RuntimeError("收藏夹 ID 解析失败")
    return str(folder_id)


def resolve_sender_uid(item: dict) -> int:
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    for key in ("mid", "uid", "up_mid"):
        value = author.get(key)
        if value:
            return int(value)
    raise RuntimeError("无法从动态详情解析 UP 主 UID")


def _extract_module_stat(item: dict) -> dict:
    modules = item.get("modules") or {}
    if isinstance(modules, dict):
        stat = modules.get("module_stat") or {}
        return stat if isinstance(stat, dict) else {}
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            if module.get("module_type") != "MODULE_TYPE_STAT" and not module.get("module_stat"):
                continue
            stat = module.get("module_stat") or {}
            if isinstance(stat, dict):
                return stat
    return {}


def _module_stat_confirms_no_favorite(stat: dict) -> bool:
    """同时有转发与评论统计但无 favorite，对应 B 站页面上无收藏按钮的动态。"""
    if not stat or "favorite" in stat:
        return False
    has_forward = any(key in stat for key in ("forward", "repost", "share"))
    has_comment = "comment" in stat
    return has_forward and has_comment


def favorite_supported(
    item: dict,
    *,
    client: BilibiliClient | None = None,
    dynamic_id: str | None = None,
) -> bool:
    """动态是否支持收藏。不确定时默认尝试收藏，避免 dynamic/detail 缺字段导致误判。"""
    stat = _extract_module_stat(item)
    if stat and "favorite" in stat:
        return True
    if stat and _module_stat_confirms_no_favorite(stat):
        if client is not None and dynamic_id:
            opus_item = fetch_opus_detail_item(client, dynamic_id)
            if opus_item:
                opus_stat = _extract_module_stat(opus_item)
                if opus_stat and "favorite" in opus_stat:
                    return True
        return False
    return True


def build_dynamic_context(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    action_text: str,
    sender_uid: int | None = None,
    strict: bool = False,
    comment_required: bool = True,
) -> DynamicContext:
    item = fetch_dynamic_detail(client, dynamic_id)
    if not item:
        raise RuntimeError("无法获取动态详情")

    referer = opus_link(dynamic_id)
    basic = item.get("basic") or {}
    if strict:
        modules = item.get("modules")
        if not isinstance(modules, dict):
            raise RuntimeError("动态详情缺少 modules 对象，停止操作")
        author = modules.get("module_author")
        if not isinstance(author, dict) or not author.get("mid"):
            raise RuntimeError("动态详情缺少 modules.module_author.mid，停止操作")
        sender_uid = int(author["mid"])
        if sender_uid <= 0:
            raise RuntimeError("modules.module_author.mid 无效，停止操作")
        if comment_required and (
            not isinstance(basic, dict)
            or not basic.get("comment_id_str")
            or not basic.get("comment_type")
        ):
            raise RuntimeError("缺少 basic.comment_id_str 或 basic.comment_type，无法确认评论对象，停止操作")
    comment_rid = str(basic.get("comment_id_str") or dynamic_id)
    comment_type = int(basic.get("comment_type") or 17)
    uid = sender_uid or resolve_sender_uid(item)

    modules = item.get("modules") or {}
    stat = modules.get("module_stat") or {}
    like_obj = stat.get("like") or {}
    liked = bool(like_obj.get("status"))

    followed = is_following(client, uid=uid, referer=referer)
    favorite_available = favorite_supported(item, client=client, dynamic_id=dynamic_id)
    favorited = (
        is_favorited(client, dynamic_id=dynamic_id, referer=referer)
        if favorite_available
        else False
    )
    chain = tuple(dynamic_chain_ids(item))
    reposted = is_reposted(client, dynamic_id=dynamic_id, referer=referer, chain_ids=chain)
    commented = has_comment(
        client,
        rid=comment_rid,
        comment_type=comment_type,
        action_text=action_text,
        referer=referer,
    ) if comment_required else False

    return DynamicContext(
        dynamic_id=dynamic_id,
        sender_uid=uid,
        referer=referer,
        comment_rid=comment_rid,
        comment_type=comment_type,
        liked=liked,
        favorited=favorited,
        favorite_available=favorite_available,
        followed=followed,
        reposted=reposted,
        commented=commented,
        chain_ids=chain,
    )


def is_following(client: BilibiliClient, *, uid: int, referer: str) -> bool:
    try:
        payload = client.request_json(RELATION_URL, params={"fid": uid}, referer=referer)
    except RuntimeError:
        return False
    if api_code(payload) != 0:
        return False
    attribute = int((payload.get("data") or {}).get("attribute") or 0)
    return attribute in FOLLOWING_ATTRIBUTES


def _opus_favorite_status(client: BilibiliClient, *, dynamic_id: str, referer: str) -> bool | None:
    try:
        payload = client.get_json(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail",
            params={
                "id": dynamic_id,
                "features": "htmlNewStyle,ugcDelete,editable,opusPrivateVisible",
            },
            referer=referer,
        )
    except RuntimeError:
        return None
    if _api_code(payload) != 0:
        return None
    item = (payload.get("data") or {}).get("item") or {}
    for module in item.get("modules") or []:
        if module.get("module_type") != "MODULE_TYPE_STAT":
            continue
        favorite = (module.get("module_stat") or {}).get("favorite") or {}
        return bool(favorite.get("status"))
    return None


def is_favorited(client: BilibiliClient, *, dynamic_id: str, referer: str) -> bool:
    status = _opus_favorite_status(client, dynamic_id=dynamic_id, referer=referer)
    return bool(status)


def is_reposted(client: BilibiliClient, *, dynamic_id: str, referer: str, chain_ids: list[str] | tuple[str, ...] = ()) -> bool:
    notice = fetch_notice_for_interact(client, dynamic_id)
    if notice:
        return bool(notice[0].get("reposted"))
    return has_reposted_in_space_feed(client, dynamic_id=dynamic_id, chain_ids=chain_ids)


def dynamic_chain_ids(item: dict | None, *, limit: int = 3) -> list[str]:
    """转发链上的动态 id：本条 → 被转发的那条 → 再往下。

    B 站把「转发型的抽奖」存回空间动态时，`orig` 会被压到链根（10-26 实测：
    黑爵转 HUGKIS 原动态 1256733200674193409，我们转发黑爵那条 1257011291170537494，
    空间动态里 orig 显示的仍是 HUGKIS 原动态 id）。只看本条 id 的话，
    「我已经转发过没有」这条核对永远查不到自己发过的那条，同一条链就会被转发两遍。
    """
    ids: list[str] = []
    node = item or {}
    for _ in range(limit):
        orig = node.get("orig") or {}
        current = str(orig.get("id_str") or "")
        if not current or current in ids:
            break
        ids.append(current)
        node = orig
    return ids


def has_reposted_in_space_feed(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    chain_ids: list[str] | tuple[str, ...] = (),
) -> bool:
    uid = get_login_uid()
    if not uid:
        return False
    referer = f"https://space.bilibili.com/{uid}/dynamic"
    offset = ""
    targets = {str(dynamic_id), *(str(x) for x in chain_ids if str(x))}
    for _ in range(SPACE_FEED_MAX_PAGES):
        try:
            payload = client.request_json(
                SPACE_FEED_URL,
                params={"host_mid": uid, "offset": offset, "type": "all"},
                referer=referer,
            )
        except RuntimeError:
            return False
        if api_code(payload) != 0:
            return False
        data = payload.get("data") or {}
        items = data.get("items") or []
        for item in items:
            if item.get("type") != "DYNAMIC_TYPE_FORWARD":
                continue
            orig = item.get("orig") or {}
            if str(orig.get("id_str") or "") in targets:
                return True
        offset = str(data.get("offset") or "")
        if not offset or len(items) < SPACE_FEED_PAGE_SIZE:
            break
    return False


def has_comment(
    client: BilibiliClient,
    *,
    rid: str,
    comment_type: int,
    action_text: str,
    referer: str,
) -> bool:
    uid = get_login_uid()
    if not uid:
        return False
    try:
        payload = client.request_json(
            REPLY_MAIN_URL,
            params={"oid": rid, "type": comment_type, "mode": 3, "next": 0, "ps": 20},
            referer=referer,
        )
    except RuntimeError:
        return False
    if api_code(payload) != 0:
        return False
    replies = (payload.get("data") or {}).get("replies") or []
    target = action_text.strip()
    for reply in replies:
        member = reply.get("member") or {}
        if str(member.get("mid")) != str(uid):
            continue
        content = reply.get("content") or {}
        message = str(content.get("message") or "")
        if target and target in message:
            return True
    return False


def like_dynamic(client: BilibiliClient, *, dynamic_id: str, csrf: str, referer: str) -> ActionResult:
    payload = client.post_json(
        LIKE_URL,
        {
            "dyn_id_str": dynamic_id,
            "up": 1,
            "spmid": "333.1369.0.0",
            "from_spmid": "333.999.0.0",
        },
        params={"csrf": csrf},
        referer=referer,
        retries=0,
        raise_on_code=False,
    )
    code = _api_code(payload)
    if code == 0:
        return ActionResult("like", True, "")
    if code == 65006:
        return ActionResult("like", True, "已赞过")
    return ActionResult("like", False, f"code={code} {_api_message(payload)}".strip(), "unknown" if code == -1 else "failed")


def is_banned_account(client: BilibiliClient, *, uid: int, referer: str) -> bool:
    """被 B 站处置过的账号会被清零粉丝数，并把官方认证名替换成 UID 本身；这种号谁都关注不了。"""
    try:
        payload = client.request_json(USER_CARD_URL, {"mid": uid, "photo": "false"}, referer=referer, retries=0)
    except (httpx.HTTPError, ValueError, TypeError):
        return False
    if _api_code(payload) != 0:
        return False
    card = (payload.get("data") or {}).get("card") or {}
    official = card.get("Official") or card.get("official") or {}
    return int(card.get("fans") or 0) == 0 and str(official.get("title") or "") == str(uid)


def follow_user(client: BilibiliClient, *, uid: int, csrf: str, referer: str) -> ActionResult:
    payload = client.post_form(
        FOLLOW_URL,
        {"fid": uid, "act": 1, "re_src": 11, "csrf": csrf},
        referer=referer,
        retries=0,
        raise_on_code=False,
    )
    code = _api_code(payload)
    if code == 0:
        # B站偶发 code=0 但关系没建立（10-09 实测 6 条），必须回读确认，补一次
        if is_following(client, uid=uid, referer=referer):
            return ActionResult("follow", True, f"uid={uid}")
        time.sleep(1.5)
        retry = client.post_form(
            FOLLOW_URL,
            {"fid": uid, "act": 1, "re_src": 11, "csrf": csrf},
            referer=referer,
            retries=0,
            raise_on_code=False,
        )
        if _api_code(retry) in (0, 22014) and is_following(client, uid=uid, referer=referer):
            return ActionResult("follow", True, f"uid={uid} 补关注成功")
        return ActionResult("follow", False, f"code=0 但关注关系未生效 uid={uid}", "failed")
    if code == 22014:
        return ActionResult("follow", True, f"uid={uid} 已关注")
    if code == 137002 and is_banned_account(client, uid=uid, referer=referer):
        return ActionResult(
            "follow", False,
            f"code=137002 且 uid={uid} 已被 B 站封禁（粉丝数清零、官方名被替换为 UID），关注条件无法满足",
            "failed",
        )
    return ActionResult("follow", False, f"code={code} {_api_message(payload)}".strip(), "unknown" if code == -1 else "failed")


def favorite_dynamic(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    csrf: str,
    referer: str,
) -> ActionResult:
    if is_favorited(client, dynamic_id=dynamic_id, referer=referer):
        return ActionResult("favorite", True, "已收藏，跳过")

    payload = client.post_json(
        COSMO_SIMPLE_ACTION_URL,
        {
            "meta": {
                "spmid": "444.42.0.0",
                "from_spmid": "333.1365.0.0",
                "from": "unknown",
            },
            "entity": {
                "object_id_str": dynamic_id,
                "type": {"biz": 2},
            },
            "action": 3,
        },
        params={"csrf": csrf},
        referer=referer,
        retries=0,
        raise_on_code=False,
    )
    code = _api_code(payload)
    message = _api_message(payload)
    if code == 0:
        for attempt in range(4):
            if is_favorited(client, dynamic_id=dynamic_id, referer=referer):
                return ActionResult("favorite", True, "")
            if attempt < 3:
                time.sleep(0.6 + attempt * 0.4)
        return ActionResult("favorite", False, message or "收藏状态未更新")
    if code in (65006, 75008):
        return ActionResult("favorite", True, message or "已收藏")
    return ActionResult("favorite", False, f"code={code} {message}".strip())


def repost_dynamic(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    my_uid: int,
    csrf: str,
    referer: str,
    content: str,
    chain_ids: list[str] | tuple[str, ...] = (),
) -> ActionResult:
    payload = client.post_form(
        REPOST_URL,
        {
            "uid": str(my_uid),
            "dynamic_id": dynamic_id,
            "content": content[:233],
            "ctrl": "[]",
            "csrf": csrf,
        },
        referer=referer,
        retries=0,
        raise_on_code=False,
    )
    code = _api_code(payload)
    if code == 0:
        return ActionResult("repost", True, content[:80])
    message = _api_message(payload)
    # 错误文案不能证明转发完成；只使用已有的账号转发状态核实。
    try:
        already_reposted = is_reposted(client, dynamic_id=dynamic_id, referer=referer, chain_ids=chain_ids)
    except (RuntimeError, ValueError, KeyError, TypeError, httpx.HTTPError):
        already_reposted = False
    if already_reposted:
        return ActionResult("repost", True, "已转发，状态已核实")
    return ActionResult("repost", False, f"code={code} {message}".strip(), "unknown" if code == -1 else "failed")


def comment_dynamic(
    client: BilibiliClient,
    *,
    rid: str,
    comment_type: int,
    message: str,
    csrf: str,
    referer: str,
) -> ActionResult:
    payload = client.post_form(
        COMMENT_URL,
        {"oid": rid, "type": comment_type, "message": message, "csrf": csrf},
        referer=referer,
        retries=0,
        raise_on_code=False,
    )
    code = _api_code(payload)
    if code == 0:
        return ActionResult("comment", True, message[:80])
    if code == 12051:
        return ActionResult("comment", True, "已有相同评论")
    return ActionResult("comment", False, f"code={code} {_api_message(payload)}".strip(), "unknown" if code == -1 else "failed")


@serial_writes
def execute_full_participation(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    sender_uid: int | None = None,
    action_text: str = DEFAULT_PARTICIPATE_TEXT,
    dry_run: bool = False,
    on_step: Callable[[int, int, str, ActionName], None] | None = None,
    action_names: tuple[ActionName, ...] = PARTICIPATION_STEPS,
    stop_on_failure: bool = False,
    strict_context: bool = False,
    journal: bool = False,
    required_actions: tuple[str, ...] | None = None,
) -> tuple[list[ActionResult], DynamicContext]:
    # 新参数是明确的动作清单，不接受猜测、重复或未实现的动作。
    if not isinstance(action_names, tuple) or not action_names:
        raise ValueError("action_names 必须是非空动作元组")
    if any(name not in PARTICIPATION_STEPS for name in action_names):
        raise ValueError("action_names 包含不支持的动作")
    if len(set(action_names)) != len(action_names):
        raise ValueError("action_names 不允许重复")

    text = (action_text or DEFAULT_PARTICIPATE_TEXT).strip() or DEFAULT_PARTICIPATE_TEXT
    saved = get_guard(dynamic_id)["progress"] if journal and not dry_run else {}
    if journal and not dry_run and get_guard("")["state"] != "ready":
        raise RuntimeError("账号队列已暂停：" + get_guard("")["reason"])
    if journal and not dry_run and get_guard(dynamic_id)["state"] != "ready":
        raise RuntimeError("活动已隔离，需核查后恢复：" + get_guard(dynamic_id)["reason"])
    context = build_dynamic_context(
        client,
        dynamic_id=dynamic_id,
        action_text=text,
        sender_uid=sender_uid,
        strict=strict_context,
        comment_required="comment" in action_names,
    )
    total_steps = len(action_names)

    planned = {
        "like": ActionResult("like", True, "跳过" if context.liked else "将点赞"),
        "follow": ActionResult("follow", True, "跳过" if context.followed else f"将关注 uid={context.sender_uid}"),
        "favorite": ActionResult(
            "favorite", True,
            "跳过" if context.favorited else (
                "无收藏入口，跳过" if not context.favorite_available else f"将收藏 rid={context.comment_rid}"
            ),
        ),
        "repost": ActionResult("repost", True, "跳过" if context.reposted else f"将转发 {text[:40]}"),
        "comment": ActionResult("comment", True, "跳过" if context.commented else f"将评论 type={context.comment_type}"),
    }
    if dry_run:
        return [planned[name] for name in action_names], context

    csrf, my_uid = require_login()
    already_done = {
        "like": context.liked,
        "follow": context.followed,
        "favorite": context.favorited or not context.favorite_available,
        "repost": context.reposted,
        "comment": context.commented,
    }
    skipped_details = {
        "like": "已点赞，跳过",
        "follow": f"uid={context.sender_uid} 已关注，跳过",
        "favorite": "已收藏，跳过" if context.favorite_available else "无收藏入口，跳过",
        "repost": "已转发，跳过",
        "comment": "已评论，跳过",
    }
    handlers = {
        "like": lambda: like_dynamic(client, dynamic_id=dynamic_id, csrf=csrf, referer=context.referer),
        "follow": lambda: follow_user(client, uid=context.sender_uid, csrf=csrf, referer=context.referer),
        "favorite": lambda: favorite_dynamic(client, dynamic_id=dynamic_id, csrf=csrf, referer=context.referer),
        "repost": lambda: repost_dynamic(
            client, dynamic_id=dynamic_id, my_uid=my_uid,
            csrf=csrf, referer=context.referer, content=text,
            chain_ids=context.chain_ids,
        ),
        "comment": lambda: comment_dynamic(
            client, rid=context.comment_rid, comment_type=context.comment_type,
            message=text, csrf=csrf, referer=context.referer,
        ),
    }
    actions: list[ActionResult] = []
    for step_index, name in enumerate(action_names, start=1):
        if on_step:
            label = ACTION_LABELS[name]
            on_step(step_index, total_steps, f"正在{label}（{step_index}/{total_steps}）", name)
        previous = saved.get(name, {})
        if previous.get("state") == "succeeded":
            result = ActionResult(name, True, "本地已确认完成，跳过", "succeeded")
        elif previous.get("state") in ("in_flight", "unknown") and not already_done[name]:
            result = ActionResult(name, False, "此前请求结果未知，需人工核查，禁止重发", "unknown")
        elif already_done[name]:
            result = ActionResult(name, True, skipped_details[name])
        else:
            if journal and get_guard("")["state"] != "ready":
                actions.append(ActionResult(name, False, "账号队列已暂停：" + get_guard("")["reason"], "failed", name in required_actions if required_actions is not None else None))
                break
            if journal:
                set_guard(dynamic_id, state="review", action=name,
                          progress={"state": "in_flight", "detail": "请求已开始，结果尚未确认", "text": text})
            try:
                result = handlers[name]()
            except (RuntimeError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
                if failure_kind(exc) == "account_paused":
                    if journal:
                        record_failure(dynamic_id, exc)
                    result = ActionResult(name, False, f"账号异常，需验证或解除限流：{exc}", "unknown")
                else:
                    result = ActionResult(name, False, f"请求结果无法确认：{exc}", "unknown")
        result.state = result.state or ("succeeded" if result.ok else "failed")
        result.required = name in required_actions if required_actions is not None else None
        actions.append(result)
        kind = failure_kind(result.detail) if not result.ok else ""
        if journal:
            set_guard(dynamic_id, action=name, progress={"state": result.state, "detail": result.detail, "text": text})
            if not result.ok:
                record_failure(dynamic_id, result.detail)
            elif all(item.get("state") not in ("unknown", "in_flight") for item in get_guard(dynamic_id)["progress"].values()):
                set_guard(dynamic_id, state="ready")
        if not result.ok and (kind == "account_paused" or result.state == "unknown" or (stop_on_failure and result.required is not False)):
            break
        if step_index < total_steps:
            time.sleep(ACTION_INTERVAL_SEC)
    if required_actions is not None:
        completed = {item.action for item in actions}
        for name in required_actions:
            if name not in completed:
                actions.append(ActionResult(name, False, "尚未执行", "pending", True))
    return actions, context
