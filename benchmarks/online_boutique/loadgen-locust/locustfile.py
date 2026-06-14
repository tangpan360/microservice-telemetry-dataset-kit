import csv
import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path

from locust import FastHttpUser, LoadTestShape, between, events, task

PRODUCTS = [
    "0PUK6V6EV0",
    "1YMWWN1N4O",
    "2ZYFJ3GM2N",
    "66VCHSJNUP",
    "6E92ZMYYFZ",
    "9SIQT8TOJO",
    "L9ECAV7KIM",
    "LS4PSXUNUM",
    "OLJCESPC7Z",
]

RUN_ID = os.getenv("OB_RUN_ID", "unknown")
SCENARIO_ID = os.getenv("OB_SCENARIO_ID", "ob-traffic-schedule")
INJECTION_EVENTS_FILE = os.getenv("OB_INJECTION_EVENTS_FILE", "")
RUN_MANIFEST_PATH = os.getenv("OB_RUN_MANIFEST_PATH", "")


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_master_runner(environment) -> bool:
    runner = getattr(environment, "runner", None)
    return runner is not None and runner.__class__.__name__ == "MasterRunner"


def append_injection_event(event_name: str, **extra) -> None:
    if not INJECTION_EVENTS_FILE:
        return
    payload = {
        "event": event_name,
        "utc": now_utc(),
        "run_id": RUN_ID,
        "scenario_id": SCENARIO_ID,
    }
    payload.update(extra)
    path = Path(INJECTION_EVENTS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


def update_run_manifest(**updates) -> None:
    if not RUN_MANIFEST_PATH:
        return
    path = Path(RUN_MANIFEST_PATH)
    manifest = {}
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_users_schedule_csv(path: str) -> tuple[int, list[int]]:
    schedule_path = Path(path)
    if not schedule_path.exists():
        raise FileNotFoundError(f"Schedule file not found: {schedule_path}")

    points: list[tuple[int, int]] = []
    with schedule_path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            t_s = int(float(row["t_s"]))
            users = int(float(row["users"]))
            points.append((t_s, max(0, users)))

    points.sort(key=lambda x: x[0])
    if not points:
        raise ValueError(f"Empty schedule file: {schedule_path}")

    if len(points) >= 2:
        step_s = max(1, points[1][0] - points[0][0])
    else:
        step_s = 10

    max_t = points[-1][0]
    size = max_t // step_s + 1
    users_by_step = [0] * size
    for t_s, users in points:
        idx = t_s // step_s
        if 0 <= idx < size:
            users_by_step[idx] = users
    return step_s, users_by_step


class ScheduleShape(LoadTestShape):
    abstract = False

    def __init__(self):
        super().__init__()
        self.duration_s = float(os.getenv("OB_SHAPE_DURATION_S", str(14 * 86400)))
        self.spawn_rate = int(os.getenv("OB_SHAPE_SPAWN_RATE", "300"))
        schedule_file = os.getenv("OB_SHAPE_FILE", "")
        if not schedule_file:
            raise ValueError("OB_SHAPE_FILE must be set")
        self.step_s, self.users_by_step = load_users_schedule_csv(schedule_file)

    def tick(self):
        run_time = self.get_run_time()
        if run_time >= self.duration_s:
            return None

        idx = int(run_time // self.step_s)
        if idx < 0:
            idx = 0
        if idx >= len(self.users_by_step):
            target_users = self.users_by_step[-1]
        else:
            target_users = self.users_by_step[idx]

        return int(target_users), max(1, int(self.spawn_rate))


def random_product() -> str:
    return random.choice(PRODUCTS)


class BoutiqueUser(FastHttpUser):
    wait_time = between(1.0, 6.0)

    def trace_headers(self):
        return {"x-run-id": RUN_ID, "x-scenario-id": SCENARIO_ID}

    def on_start(self):
        self.client.get("/", headers=self.trace_headers(), name="/")

    @task(5)
    def browse(self):
        self.client.get("/", headers=self.trace_headers(), name="/")
        self.client.get(
            f"/product/{random_product()}",
            headers=self.trace_headers(),
            name="/product/[id]",
        )

    @task(3)
    def cart_add(self):
        self.client.post(
            "/cart",
            data={"product_id": random_product(), "quantity": random.randint(1, 3)},
            headers=self.trace_headers(),
            name="/cart:add",
        )
        self.client.get("/cart", headers=self.trace_headers(), name="/cart")

    @task(1)
    def checkout(self):
        # Ensure the cart is not empty; otherwise frontend returns 422.
        product = random_product()
        self.client.get(
            f"/product/{product}",
            headers=self.trace_headers(),
            name="/product/[id]",
        )
        self.client.post(
            "/cart",
            data={"product_id": product, "quantity": random.randint(1, 3)},
            headers=self.trace_headers(),
            name="/cart:add",
        )
        self.client.get("/cart", headers=self.trace_headers(), name="/cart")
        self.client.post(
            "/cart/checkout",
            data={
                "email": f"user{random.randint(1, 10_000_000)}@example.com",
                "street_address": "1 Main St",
                "zip_code": "94043",
                "city": "Mountain View",
                "state": "CA",
                "country": "United States",
                "credit_card_number": "4111111111111111",
                "credit_card_expiration_month": random.randint(1, 12),
                "credit_card_expiration_year": random.randint(2027, 2097),
                "credit_card_cvv": "123",
            },
            headers=self.trace_headers(),
            name="/cart/checkout",
        )


@events.test_start.add_listener
def announce_config(environment, **kwargs):
    shape_file = os.getenv("OB_SHAPE_FILE", "")
    shape_duration_s = os.getenv("OB_SHAPE_DURATION_S", "")
    shape_spawn_rate = os.getenv("OB_SHAPE_SPAWN_RATE", "")
    print(
        json.dumps(
            {
                "scenario_id": SCENARIO_ID,
                "run_id": RUN_ID,
                "shape": "schedule_file",
                "shape_file": shape_file,
                "shape_duration_s": shape_duration_s,
                "shape_spawn_rate": shape_spawn_rate,
            },
            ensure_ascii=True,
        )
    )
    if not is_master_runner(environment):
        return
    append_injection_event(
        "schedule_started",
        shape_file=shape_file,
        shape_duration_s=shape_duration_s,
        shape_spawn_rate=shape_spawn_rate,
    )
    update_run_manifest(
        status="running",
        inject_start_utc=now_utc(),
    )


@events.test_stop.add_listener
def record_test_stop(environment, **kwargs):
    if not is_master_runner(environment):
        return
    append_injection_event("schedule_finished")
    update_run_manifest(
        status="finished",
        inject_end_utc=now_utc(),
    )

