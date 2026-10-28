


def source_mids() -> set[int]:
    """9 个数据源 UP 自己的 mid。他们发的图文/专栏是「容器或公告」，
    不是可以去转发的抽奖；这类链接应该在入库前就被丢掉，而不是等人工发现。"""
    from src.sources import (ds1_xiaozhuli, ds2_fanqiao, ds3_gongjuren, ds4_junming,
                             ds5_hudong, ds6_nuomi, ds7_dajinli, ds8_qianchoujun, ds9_jinlisongrui)

    out = set()
    for mod in (ds1_xiaozhuli, ds2_fanqiao, ds3_gongjuren, ds4_junming, ds5_hudong,
                ds6_nuomi, ds7_dajinli, ds8_qianchoujun, ds9_jinlisongrui):
        try:
            out.add(int(mod.MID))
        except (AttributeError, TypeError, ValueError):
            continue
    return out
