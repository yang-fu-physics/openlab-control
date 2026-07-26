from __future__ import annotations

import asyncio
import hmac
import re
import time
from typing import Literal

import nonebot
from fastapi import FastAPI, Header, HTTPException
from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Bot
from nonebot.exception import NoBotFound
from nonebot.log import logger
from pydantic import BaseModel, Field

from .routing import parse_qqs, recipients


__plugin_meta__ = nonebot.plugin.PluginMetadata(
    name="OpenLab 报警接收器",
    description=(
        "通过带 Token 的 HTTP 接口接收 OpenLab Control "
        "Warning/Error，并按角色推送到 QQ"
    ),
    usage="POST /alarm/report",
    config=None,
)


class AlarmPayload(BaseModel):
    event_id: str = Field(
        ...,
        min_length=16,
        max_length=128,
        description="发射端生成的稳定事件 ID，用于重试去重",
    )
    level: Literal["warning", "error"] = Field(
        ...,
        description="warning 只发测试员；error 发管理员和测试员",
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="报警正文",
    )


driver = nonebot.get_driver()
global_config = driver.config
app: FastAPI = driver.server_app

ALARM_TOKEN = str(
    getattr(global_config, "alarm_token", "")
    or ""
).strip()
DELIVERY_TTL_SECONDS = max(
    60.0,
    float(
        getattr(
            global_config,
            "alarm_delivery_ttl_seconds",
            86400.0,
        )
    ),
)


ADMIN_QQS = parse_qqs(
    getattr(global_config, "alarm_admin_qqs", ())
)
TESTER_QQS = parse_qqs(
    getattr(global_config, "alarm_tester_qqs", ())
)

_delivery_lock = asyncio.Lock()
_delivered: dict[str, tuple[float, set[int]]] = {}


def _forget_expired(now: float) -> None:
    expired = [
        event_id
        for event_id, (timestamp, _recipients_sent)
        in _delivered.items()
        if now - timestamp > DELIVERY_TTL_SECONDS
    ]
    for event_id in expired:
        _delivered.pop(event_id, None)


@app.post("/alarm/report", tags=["报警接收"])
async def receive_alarm(
    payload: AlarmPayload,
    x_token: str | None = Header(
        default=None,
        alias="X-Token",
        description="报警接口令牌",
    ),
):
    if not ALARM_TOKEN:
        logger.error(
            "报警接口拒绝请求：服务端未配置 alarm_token"
        )
        raise HTTPException(
            status_code=503,
            detail="Alarm receiver token is not configured",
        )
    if not hmac.compare_digest(
        x_token or "",
        ALARM_TOKEN,
    ):
        logger.warning("接收到无效 Token 的报警请求")
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing token",
        )
    if not re.fullmatch(
        r"[A-Za-z0-9._:-]{16,128}",
        payload.event_id,
    ):
        raise HTTPException(
            status_code=422,
            detail="Invalid event_id",
        )

    targets = recipients(
        payload.level,
        ADMIN_QQS,
        TESTER_QQS,
    )
    if not targets:
        logger.error(
            "报警 {} 没有配置接收人",
            payload.level,
        )
        raise HTTPException(
            status_code=503,
            detail="No recipients configured for this alarm level",
        )
    try:
        bot: Bot = get_bot()
    except NoBotFound as exc:
        logger.error("没有可用的 OneBot V11 Bot 实例")
        raise HTTPException(
            status_code=503,
            detail="Bot not connected",
        ) from exc

    failures: list[int] = []
    async with _delivery_lock:
        now = time.monotonic()
        _forget_expired(now)
        timestamp, delivered = _delivered.get(
            payload.event_id,
            (now, set()),
        )
        pending = sorted(targets - delivered)
        for qq in pending:
            try:
                await bot.send_private_msg(
                    user_id=qq,
                    message=payload.message,
                )
            except Exception:
                failures.append(qq)
                logger.exception(
                    "发送报警到 QQ {} 失败",
                    qq,
                )
            else:
                delivered.add(qq)
                logger.info(
                    "成功发送 {} 报警到 QQ {}",
                    payload.level,
                    qq,
                )
        _delivered[payload.event_id] = (
            timestamp,
            delivered,
        )

    if failures:
        raise HTTPException(
            status_code=502,
            detail=(
                "Alarm delivery failed for one or more "
                "configured recipients"
            ),
        )
    return {
        "status": "success",
        "level": payload.level,
        "delivered": len(targets),
        "duplicate": not pending,
    }


@driver.on_startup
async def _startup_notice():
    if not ALARM_TOKEN:
        logger.error(
            "【OpenLab 报警插件】未配置 alarm_token；"
            "接口将 fail-closed，所有请求返回 503"
        )
    if not TESTER_QQS:
        logger.warning(
            "【OpenLab 报警插件】未配置 alarm_tester_qqs；"
            "Warning 无接收人"
        )
    if not ADMIN_QQS:
        logger.warning(
            "【OpenLab 报警插件】未配置 alarm_admin_qqs；"
            "Error 仍只会发给测试员"
        )
    logger.info(
        "OpenLab 报警接收器已加载：http://{}:{}/alarm/report",
        global_config.host,
        global_config.port,
    )
