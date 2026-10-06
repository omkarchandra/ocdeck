#!/usr/bin/env python3
"""Render the README screenshots from made-up demo data.

Nothing here reads your sessions, projects or machine: the dashboard is fed a
synthetic snapshot, so the images contain no personal information.

    python scripts/make_screenshots.py            # writes docs/screenshots/*.svg
    python scripts/make_screenshots.py --png      # also converts them with inkscape
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import time
from pathlib import Path

from ocdeck.app import OCDeckApp
from ocdeck.models import (
    DashboardSnapshot,
    ProjectRecord,
    ServiceRecord,
    SessionRecord,
    SystemMetrics,
)

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
SIZE = (150, 50)
MINUTE = 60_000


def demo_snapshot() -> DashboardSnapshot:
    now = int(time.time() * 1000)

    def session(identifier, title, project, directory, minutes, **fields):
        return SessionRecord(
            id=identifier,
            title=title,
            directory=f"/home/demo/{directory}",
            project_id=project,
            created_ms=now - (minutes + 90) * MINUTE,
            updated_ms=now - minutes * MINUTE,
            last_interaction_ms=now - minutes * MINUTE,
            **fields,
        )

    sessions = (
        session("ses_a1", "Add Stripe checkout", "p1", "web-shop", 1, status="busy",
                instance_count=1, terminals=("oc-ses_a1",), terminal_attached=True,
                harness="claude", model="claude-opus-5-5", assistant_active=True,
                assistant_activity_ms=now - 20_000, last_prompt="Wire the checkout page to Stripe"),
        session("ses_a2", "Fix cart rounding bug", "p1", "web-shop", 6, instance_count=1,
                terminals=("oc-ses_a2",), terminal_attached=True, harness="opencode",
                model="gpt-6-astra", permission="bash: npm test -- cart",
                permission_id="per_demo1"),
        session("ses_b1", "Tune learning-rate sweep", "p2", "ml-notes", 14, instance_count=1,
                terminals=("cx-ses_b1",), terminal_attached=True, harness="codex",
                model="gpt-6-astra", background_jobs=("python sweep.py --grid lr",)),
        session("ses_b2", "Write evaluation harness", "p2", "ml-notes", 55, harness="claude",
                model="claude-sonnet-5-5"),
        session("ses_c1", "Rate limiter design", "p3", "api-gateway", 3, instance_count=1,
                terminals=("cc-ses_c1",), terminal_attached=True, harness="claude",
                model="claude-opus-5-5", question="Use Redis or in-memory counters?"),
        session("ses_c2", "Migrate to v2 auth", "p3", "api-gateway", 38, harness="opencode",
                model="gpt-6-astra"),
        session("ses_d1", "Refresh install guide", "p4", "docs-site", 190, harness="opencode",
                model="gpt-6-sol"),
    )
    projects = (
        ProjectRecord(id="p1", directory="/home/demo/web-shop", name="web-shop", session_count=2,
                      active_count=1, attached_count=2, instance_count=2, updated_ms=now - MINUTE,
                      git_branch="feature/checkout", git_dirty=4),
        ProjectRecord(id="p2", directory="/home/demo/ml-notes", name="ml-notes", session_count=2,
                      attached_count=1, instance_count=1, updated_ms=now - 14 * MINUTE,
                      git_branch="main", git_dirty=0),
        ProjectRecord(id="p3", directory="/home/demo/api-gateway", name="api-gateway",
                      session_count=2, attached_count=1, instance_count=1,
                      updated_ms=now - 3 * MINUTE, git_branch="rate-limit", git_dirty=2),
        ProjectRecord(id="p4", directory="/home/demo/docs-site", name="docs-site", session_count=1,
                      updated_ms=now - 190 * MINUTE, git_branch="main", git_dirty=0),
    )
    return DashboardSnapshot(
        sessions=sessions,
        projects=projects,
        services=(
            ServiceRecord(unit="opencode-web.service", label="OpenCode Web",
                          role="local API and web client", state="active"),
        ),
        metrics=SystemMetrics(memory_percent=38, load_1m=1.2, cpu_count=8),
        connection="live",
        connection_detail="demo data",
    )


class DemoSource:
    opencode_bin = None

    async def collect(self) -> DashboardSnapshot:
        return demo_snapshot()


async def capture(views: dict[str, str]) -> dict[str, str]:
    app = OCDeckApp(DemoSource(), auto_refresh=False)
    shots: dict[str, str] = {}
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        for name, key in views.items():
            await pilot.press(key)
            await pilot.pause(0.4)
            shots[name] = app.export_screenshot(title="OC Deck")
    return shots


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--png", action="store_true", help="also convert to PNG with inkscape")
    arguments = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    shots = asyncio.run(capture({"operations": "1", "agents": "4"}))
    for name, svg in shots.items():
        path = OUTPUT / f"{name}.svg"
        path.write_text(svg, encoding="utf-8")
        print(path)
        if arguments.png:
            inkscape = shutil.which("inkscape")
            if not inkscape:
                raise SystemExit("inkscape is required for --png")
            subprocess.run([inkscape, str(path), "--export-type=png", "--export-dpi=110",
                            f"--export-filename={path.with_suffix('.png')}"], check=True,
                           capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
