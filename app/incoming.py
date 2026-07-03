import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)


def _as_updates(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    return [payload]


def _message_text(update: dict[str, Any]) -> str:
    envelope = update.get("envelope")
    if isinstance(envelope, dict):
        data_message = envelope.get("dataMessage")
        if isinstance(data_message, dict):
            return str(data_message.get("message") or "")

        sync_message = envelope.get("syncMessage")
        if isinstance(sync_message, dict):
            sent_message = sync_message.get("sentMessage")
            if isinstance(sent_message, dict):
                return str(sent_message.get("message") or "")

    data_message = update.get("dataMessage")
    if isinstance(data_message, dict):
        return str(data_message.get("message") or "")

    return str(update.get("message") or "")


def iter_received_messages(payload: Any) -> Iterable[dict[str, Any]]:
    for update in _as_updates(payload):
        if not isinstance(update, dict):
            continue

        text = _message_text(update).strip()
        if text:
            yield {"text": text, "raw": update}


def handle_received_message(message: dict[str, Any]) -> None:
    text = message["text"]
    if text.startswith("!"):
        logger.info("Received Signal bot command candidate: %s", text.split(maxsplit=1)[0])
        return

    logger.debug("Received Signal message without command prefix")


def handle_signal_updates(payload: Any) -> int:
    handled = 0
    for message in iter_received_messages(payload):
        handle_received_message(message)
        handled += 1
    return handled
