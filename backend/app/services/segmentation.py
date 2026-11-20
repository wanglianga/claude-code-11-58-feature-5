"""运行切分:根据列车、编组、线路、区段、停车与折返记录形成可计量运行片段。

切断条件(任一发生即闭合当前片段):
- 停车 STOP / 起动 START_MOVE
- 折返 TURNBACK(方向翻转)
- 离开区段 EXIT_SECTION
- 换编组 CONSIST_CHANGE
- 车载表倍率变化(进入不同倍率区间)
- 车载设备重启/读数倒退
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..models import (Device, MeterReading, Run, RunConsist, RunEvent, RunSegment,
                      Section)

CUT_AT_EVENT = {"STOP", "TURNBACK", "EXIT_SECTION", "CONSIST_CHANGE"}


def _onboard_device(db: Session, run: Run) -> Device | None:
    return (db.query(Device)
            .filter(Device.kind == "ONBOARD", Device.train_id == run.train_id)
            .order_by(Device.id).first())


def _consist_at(db: Session, run: Run, ts: datetime) -> int:
    rc = (db.query(RunConsist)
          .filter(RunConsist.run_id == run.id, RunConsist.valid_from <= ts)
          .filter((RunConsist.valid_to.is_(None)) | (RunConsist.valid_to > ts))
          .order_by(RunConsist.valid_from.desc()).first())
    if rc:
        return rc.consist_id
    rc = (db.query(RunConsist).filter(RunConsist.run_id == run.id)
          .order_by(RunConsist.valid_from).first())
    return rc.consist_id


def segment_run(db: Session, run: Run) -> list[RunSegment]:
    """重建某运行版本的全部片段(幂等:先删后建)。"""
    db.flush()  # 确保未落库的事件/读数可见(会话关闭 autoflush)
    db.query(RunSegment).filter(RunSegment.run_id == run.id).delete()

    events = (db.query(RunEvent).filter(RunEvent.run_id == run.id)
              .order_by(RunEvent.ts, RunEvent.id).all())
    device = _onboard_device(db, run)
    readings = []
    if device:
        readings = (db.query(MeterReading)
                    .filter(MeterReading.device_id == device.id,
                            MeterReading.sampled_at >= run.started_at,
                            MeterReading.sampled_at <= run.ended_at)
                    .order_by(MeterReading.sampled_at, MeterReading.seq).all())

    # 设备侧切断点:倍率变化 / 读数倒退(重启)
    device_cuts: list[tuple[datetime, str]] = []
    prev = None
    for r in readings:
        if prev is not None:
            if r.multiplier != prev.multiplier:
                device_cuts.append((r.sampled_at, "MULTIPLIER_CHANGE"))
            elif (r.traction_cum is not None and prev.traction_cum is not None
                  and r.traction_cum < prev.traction_cum - 1e-9):
                device_cuts.append((r.sampled_at, "DEVICE_RESET"))
        prev = r

    segments: list[RunSegment] = []
    cur = None  # dict(section_id, direction, start)
    seq = 0

    def close(ts: datetime, reason: str):
        nonlocal cur, seq
        if cur and ts > cur["start"]:
            seq += 1
            segments.append(RunSegment(
                run_id=run.id, section_id=cur["section_id"],
                consist_id=_consist_at(db, run, cur["start"]),
                direction=cur["direction"], start_ts=cur["start"], end_ts=ts,
                seq=seq, cut_reason=reason))
        cur = None

    def open_seg(ts: datetime, section_id: int, direction: str):
        nonlocal cur
        cur = {"section_id": section_id, "direction": direction, "start": ts}

    # 合并事件与设备切断点,按时间扫描
    timeline: list[tuple[datetime, str, RunEvent | None]] = []
    for ev in events:
        timeline.append((ev.ts, ev.type, ev))
    for ts, reason in device_cuts:
        timeline.append((ts, reason, None))
    timeline.sort(key=lambda x: (x[0], 0 if x[2] is None else 1))

    for ts, kind, ev in timeline:
        if kind == "ENTER_SECTION":
            if cur is None:
                open_seg(ts, ev.section_id, ev.direction or "UP")
        elif kind == "START_MOVE":
            if cur is None and ev and ev.section_id:
                open_seg(ts, ev.section_id, ev.direction or (segments[-1].direction if segments else "UP"))
        elif kind in CUT_AT_EVENT:
            reason = {"STOP": "STOP", "TURNBACK": "TURNBACK",
                      "EXIT_SECTION": "SECTION_EXIT",
                      "CONSIST_CHANGE": "CONSIST_CHANGE"}[kind]
            if kind == "CONSIST_CHANGE" and cur:
                # 换编组:同区段同方向立即以新编组重开
                section_id, direction = cur["section_id"], cur["direction"]
                close(ts, reason)
                open_seg(ts, section_id, direction)
            else:
                close(ts, reason)
                if kind == "TURNBACK" and ev and ev.section_id:
                    # 折返后立即以反方向开启新片段
                    open_seg(ts, ev.section_id, "DOWN" if (ev.direction or "UP") == "UP" else "UP")
        elif kind in ("MULTIPLIER_CHANGE", "DEVICE_RESET"):
            # 设备切断:同区段同方向立即重开
            if cur:
                section_id, direction = cur["section_id"], cur["direction"]
                close(ts, kind)
                open_seg(ts, section_id, direction)

    close(run.ended_at, "END")
    for s in segments:
        db.add(s)
    db.flush()
    return segments
