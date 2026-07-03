import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import schedule

from signal_api import SignalAPI

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
RETRY_QUEUE_PATH = Path(os.getenv("MESSAGE_RETRY_QUEUE_PATH", "/data/pending_messages.json"))
RETRY_FOR = timedelta(hours=24)


def _retry_interval_seconds():
    try:
        interval = int(os.getenv("MESSAGE_RETRY_INTERVAL_SECONDS", "300"))
    except ValueError:
        logger.warning("Invalid MESSAGE_RETRY_INTERVAL_SECONDS, falling back to 300")
        return 300

    if interval <= 0:
        logger.warning("MESSAGE_RETRY_INTERVAL_SECONDS must be positive, falling back to 300")
        return 300

    return interval

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)
RETRY_INTERVAL_SECONDS = _retry_interval_seconds()

GROUP_DEV = "group.eElWcFNVSk4zbVJIbVVQb3dERjU2bzlOTjlCZDJBTDZQZDMxblArT2ZHdz0="
GROUP_FREITAGSPIZZA = "group.QkI2c0tWRFpRTW5OVmFvU2xBRUNXR1NxREErQzBtL3kySmt6bTNtZVRuMD0="
NUMBER = "+4970329189305"

signal = SignalAPI([GROUP_FREITAGSPIZZA], NUMBER)


def _now():
    return datetime.now(timezone.utc)


def _parse_timestamp(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return _now()


def _load_retry_queue():
    if not RETRY_QUEUE_PATH.exists():
        return []

    try:
        with RETRY_QUEUE_PATH.open("r", encoding="utf-8") as queue_file:
            queue = json.load(queue_file)
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not load retry queue from %s", RETRY_QUEUE_PATH)
        return []

    if not isinstance(queue, list):
        logger.error("Retry queue at %s is not a list, ignoring it", RETRY_QUEUE_PATH)
        return []

    return queue


def _save_retry_queue(queue):
    RETRY_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = RETRY_QUEUE_PATH.with_suffix(RETRY_QUEUE_PATH.suffix + ".tmp")

    try:
        with temp_path.open("w", encoding="utf-8") as queue_file:
            json.dump(queue, queue_file, ensure_ascii=False, indent=2)
        temp_path.replace(RETRY_QUEUE_PATH)
    except OSError:
        logger.exception("Could not save retry queue to %s", RETRY_QUEUE_PATH)


def _queue_for_retry(message, job_name):
    now = _now().isoformat()
    queue = _load_retry_queue()
    queue.append({
        "id": str(uuid.uuid4()),
        "message": message,
        "job_name": job_name,
        "first_failed_at": now,
        "last_attempt_at": now,
        "attempts": 1,
    })
    _save_retry_queue(queue)
    logger.warning(
        "Queued failed message from %s for retry for up to %s hour(s)",
        job_name,
        int(RETRY_FOR.total_seconds() / 3600),
    )


def send_message(message, job_name, retry_on_failure=True):
    if signal.send_message(message):
        return True

    if retry_on_failure:
        _queue_for_retry(message, job_name)
    else:
        logger.warning("Message from %s failed and is not configured for retry", job_name)

    return False


def process_retry_queue():
    queue = _load_retry_queue()
    if not queue:
        return

    logger.info("Processing %d queued message(s)", len(queue))
    remaining = []
    now = _now()

    for item in queue:
        first_failed_at = _parse_timestamp(item.get("first_failed_at"))
        if now - first_failed_at > RETRY_FOR:
            logger.error(
                "Dropping queued message %s from %s after retry window expired",
                item.get("id"),
                item.get("job_name", "unknown job"),
            )
            continue

        if signal.send_message(item.get("message", "")):
            logger.info(
                "Successfully re-sent queued message %s from %s after %d attempt(s)",
                item.get("id"),
                item.get("job_name", "unknown job"),
                item.get("attempts", 0) + 1,
            )
            continue

        item["attempts"] = item.get("attempts", 0) + 1
        item["last_attempt_at"] = now.isoformat()
        remaining.append(item)

    _save_retry_queue(remaining)


def remind_for_pizza():
    message = "🍕🍕🍕\nBitte bestellt bis morgen 16:00 eure Pizza auf https://food.aerospace-lab.de/grouporders"
    logger.info("Running remind_for_pizza job")
    send_message(message, "remind_for_pizza")


def last_remind_for_pizza():
    message = "Letzter Aufruf für die Pizza-Bestellung! T-00:30:00 🍕"
    logger.info("Running last_remind_for_pizza job")
    send_message(message, "last_remind_for_pizza")

    message = "Ich gehe heute Pizza kaufen 👑\n(Bitte eine Person reagieren)"
    logger.info("Sending follow-up pizza pickup message")
    send_message(message, "last_remind_for_pizza_followup")


def remind_to_eat():
    message = "Guten Appetit! 🍕"
    logger.info("Running remind_to_eat job")
    send_message(message, "remind_to_eat", retry_on_failure=False)


def trust_all_recipients():
    logger.info("Running trust_all_recipients job")
    signal.trust_all()


def process_updates():
    ''' 
    #TODO: receive updates and process them
    '''
    # if used, disable AUTO_RECEIVE_SCHEDULE in docker-compose.yml
    pass


# schedule.every().minute.do(process_updates)

schedule.every().thursday.at("14:50", "Europe/Berlin").do(trust_all_recipients)
schedule.every().thursday.at("15:00", "Europe/Berlin").do(remind_for_pizza)
schedule.every().friday.at("15:30", "Europe/Berlin").do(last_remind_for_pizza)
schedule.every().friday.at("20:30", "Europe/Berlin").do(remind_to_eat)
schedule.every(RETRY_INTERVAL_SECONDS).seconds.do(process_retry_queue)

logger.info("Signal bot started with log level %s", LOG_LEVEL)
logger.info("Scheduled %d jobs", len(schedule.get_jobs()))
logger.info("Retry queue path: %s", RETRY_QUEUE_PATH)
process_retry_queue()

while True:
    try:
        schedule.run_pending()
    except Exception:
        logger.exception("Error while running scheduled jobs")
    time.sleep(1)
