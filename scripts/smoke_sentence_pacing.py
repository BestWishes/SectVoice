from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from uuid import UUID

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sectvoice.app import build_services
from sectvoice.core.runtime import _pause_milliseconds
from sectvoice.domain import PayloadStatus, SynthesisSettings, Tier
from sectvoice.paths import AppPaths


CASES = (
    "“砰——”",
    "到了。",
    "真是无事一身轻。",
    "他的目的地，还是太一城。",
    "此时许川的心情已经没有之前那般急迫和焦躁。",
    "回到住处，许川收拾好东西，再度走出了圣地山门。",
    "那他就又要回到那种苦于缺少修炼资源，进境缓慢的日子中去了。",
    "作为一座人族巨城，太一城每日的人流量达到了一个恐怖的地步，根本不是圣地内的弟子能比得上的。",
    "而如今，他修炼玄阳决，铸就雄厚根基，武道之路不说一路平坦，至少不会在一个境界浪费几年光阴，而不得。",
)

ALTERNATE_CASES = (
    "出发。",
    "窗外的雨停了。",
    "他把书页轻轻合上。",
    "晨光越过窗沿，落在安静的木桌上。",
    "院中的竹叶微微摇动，声音清楚而又平稳。",
    "她沿着河岸慢慢向前走，远处的灯火依次亮了起来。",
    "山路转过一道弯以后，开阔的谷地和安静的村庄同时出现在眼前。",
    "天色渐暗，归来的行人穿过长街，店铺逐一掌灯，整座小城仍旧从容而安定。",
)

FEEDBACK_CASES = (
    "苏妙音闻言愣了愣，疑云布满她略显苍白的脸，不过倒是没有之前那般害怕。",
    "“原来是圣地高徒，怪不得怪不得……”",
    "太一圣地是这东域顶尖的宗门，名气颇大，很多人挤破脑袋都想要加入。",
    "“他们为什么要抓你回去？”",
    "许川点点头。",
    "他之前已经从系统的信息之中了解过事情的大致经过了，只是没有她所讲的这么详细罢了。",
    "就是不知道她从哪知道的这个消息罢了。",
    "将三名林家弟子的尸体搜刮一番，搜出来几十块下品元石和几瓶疗伤丹药。",
)

FIELD_FEEDBACK_CASES = (
    "普通人几乎没有机会能够进入秘境，因为整个东域的秘境不是掌握在各大圣地和宗门手中就是在各个家族手中，根本不会对外开放。",
    "“这个秘境是我家先祖所发现，但是先祖他不被秘境钥匙所认可，终其一生也没能进入这个秘境。”",
    "各种妖兽频出，就算是许川，也没办法保证自身的安全，更别说还带着一个淬体境五重的累赘。",
    "到了。",
    "许川点点头。",
    "窗外的雨停了。",
    "晨光越过窗沿，落在安静的木桌上。",
)

NIGHTFALL_FEEDBACK_CASES = (
    "趁着晚霞，许川将《赤血刀法》的秘籍拿出来。",
    "准备先修炼一番。",
    "秘境之中有机缘，也有危险。",
    "多一门攻击手段也更能保证自己和苏妙音的安全。",
)

REPORTED_VALIDATION_FAILURE_CASES = (
    "收到罗同的命令，外面走近来几个执法堂弟子，拖着跪在地上的几人就要前往地牢。",
    "“大人饶命！”",
    "“不关我们的事啊！”",
    "等到这里彻底安静下来之后，罗同对着空无一人的宅院说道。",
    "“我明白，父亲！”",
    "一道声音传来。",
)

MARKDOWN_LAYOUT_CASES = (
    "> 你又走不了、反击成本高 → 我的攻击成本很低。",
    "那么**地位高的人欺负地位低的人，不只是“权力使人变坏”，而是某些原本限制攻击路径的成本消失了。**",
    "普通文字也必须在排版段落之后继续朗读。",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="真实Reader长短句节奏与完整性验收")
    parser.add_argument("--tier", choices=("basic", "standard"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--voice-id", type=UUID)
    parser.add_argument("--paragraph-pause-ms", type=int, default=280)
    parser.add_argument(
        "--case-set",
        choices=(
            "base",
            "alternate",
            "feedback",
            "field",
            "nightfall",
            "reported-validation",
            "markdown-layout",
        ),
        default="base",
    )
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(sys.argv[:1])
    services = build_services(AppPaths.discover())
    tier = Tier(args.tier)
    active = services.packages.active_for(tier)
    if active is None:
        raise RuntimeError(f"{tier.value}语音包没有启用")
    voice = next(
        (
            profile
            for profile in services.voices.list_profiles()
            if (args.voice_id is None or profile.voice_id == args.voice_id)
            if any(
                payload.tier is tier
                and payload.engine_id == active.engine_id
                and payload.status is PayloadStatus.READY
                for payload in services.voices.payloads_for(profile.voice_id)
            )
        ),
        None,
    )
    if voice is None:
        raise RuntimeError("没有READY的真实声音Payload")
    cases = {
        "base": CASES,
        "alternate": ALTERNATE_CASES,
        "feedback": FEEDBACK_CASES,
        "field": FIELD_FEEDBACK_CASES,
        "nightfall": NIGHTFALL_FEEDBACK_CASES,
        "reported-validation": REPORTED_VALIDATION_FAILURE_CASES,
        "markdown-layout": MARKDOWN_LAYOUT_CASES,
    }[args.case_set]
    source_text = "\n".join(cases)
    document = services.documents.create(f"{tier.value}长短句节奏验收", source_text)
    expected_units = tuple(
        unit for unit in document.mapping.units if any(char.isalnum() for char in unit.text)
    )
    expected_ids = tuple(str(unit.speech_unit_id) for unit in expected_units)
    by_id = {str(unit.speech_unit_id): unit for unit in expected_units}
    controller = services.playback
    controller.set_document(document)
    controller.set_voice(voice.voice_id, tier)
    settings = SynthesisSettings(
        volume=0.0,
        punctuation_pause_ms=120,
        paragraph_pause_ms=args.paragraph_pause_ms,
    )
    controller.set_settings(settings)
    launched_at = time.perf_counter()
    starts: list[dict[str, object]] = []
    errors: list[str] = []
    underruns = 0
    finished_at: float | None = None

    def unit_started(unit_id: str, _start: int, _end: int) -> None:
        unit = by_id[unit_id]
        starts.append(
            {
                "unit_id": unit_id,
                "at_seconds": time.perf_counter() - launched_at,
                "text": unit.text,
                "characters": sum(character.isalnum() for character in unit.text),
            }
        )

    def underflow() -> None:
        nonlocal underruns
        underruns += 1

    def failed(message: str) -> None:
        errors.append(message)
        app.quit()

    def finished() -> None:
        nonlocal finished_at
        finished_at = time.perf_counter() - launched_at
        app.quit()

    controller.currentUnitChanged.connect(unit_started)
    controller.audio.underrun.connect(underflow)
    controller.error.connect(failed)
    controller.finished.connect(finished)
    QTimer.singleShot(0, controller.play)
    QTimer.singleShot(int(args.timeout * 1000), lambda: failed("测试超时"))
    app.exec()

    end_time = finished_at or time.perf_counter() - launched_at
    for index, item in enumerate(starts):
        next_time = (
            float(starts[index + 1]["at_seconds"])
            if index + 1 < len(starts)
            else end_time
        )
        interval = max(0.001, next_time - float(item["at_seconds"]))
        pause_seconds = _pause_milliseconds(str(item["text"]), settings) / 1000.0
        speech_interval = max(0.001, interval - pause_seconds)
        item["start_to_next_seconds"] = interval
        item["configured_pause_seconds"] = pause_seconds
        item["speech_interval_seconds"] = speech_interval
        item["characters_per_interval_second"] = float(item["characters"]) / interval
        item["characters_per_speech_second"] = (
            float(item["characters"]) / speech_interval
        )

    actual_ids = tuple(str(item["unit_id"]) for item in starts)
    controller.stop()
    services.asr.shutdown()
    services.engines.shutdown()
    with services.documents.database.connect() as connection:
        connection.execute("DELETE FROM documents WHERE document_id=?", (str(document.document_id),))
    measured_units = [item for item in starts if int(item["characters"]) >= 5]
    rates = [
        float(item["characters_per_speech_second"])
        for item in measured_units
    ]
    # Very short quoted exclamations legitimately spend proportionally more
    # time on their onset/release.  Keep a lower but still bounded floor for
    # 5-8 character units instead of declaring natural emphasis a failure.
    pace_in_bounds = bool(measured_units) and all(
        float(item["characters_per_speech_second"])
        >= (2.5 if int(item["characters"]) <= 8 else 2.8)
        and float(item["characters_per_speech_second"]) <= 6.2
        for item in measured_units
    )
    passed = (
        not errors
        and underruns == 0
        and actual_ids == expected_ids
        and pace_in_bounds
    )
    result = {
        "passed": passed,
        "tier": tier.value,
        "voice_id": str(voice.voice_id),
        "elapsed_seconds": end_time,
        "expected_units": len(expected_ids),
        "started_units": len(actual_ids),
        "exactly_once_in_order": actual_ids == expected_ids,
        "underruns": underruns,
        "minimum_rate": min(rates) if rates else None,
        "maximum_rate": max(rates) if rates else None,
        "errors": errors,
        "units": starts,
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    print(serialized)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized + "\n", encoding="utf-8")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
