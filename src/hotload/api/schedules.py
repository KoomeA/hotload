"""Scheduled recording: "record this stream when it goes live" (see scheduler.py for how a schedule is released)."""

import logging
import time
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .. import engine, metrics, ratelimit
from ..config import Settings
from ..logging_setup import log

logger = logging.getLogger("hotload.schedules")
MIN_LEAD = 5  # seconds: a start time any closer than this is "now", which is what Record live stream is for


class ScheduleIn(BaseModel):
    url: str = Field(max_length=2048)
    start_at: datetime | None = None  # with a timezone; omitted = use the start time the page itself announces
    minutes: int = Field(ge=1, le=240)
    preset: str = "best_mp4"
    age_confirmed: bool = False


def add_schedule_routes(app: FastAPI, settings: Settings) -> None:
    async def owner(request: Request):
        user, tier, _key = await app.state.who(request)
        if user is None:
            raise HTTPException(401, "Sign in to schedule recordings")
        return user, tier

    def view(row, job=None) -> dict:
        data = row.public()
        if job is not None:
            data["job"] = {"state": job.state, "progress": job.progress, "message": job.message, "title": job.title,
                           "download_url": f"/api/v1/jobs/{job.id}/file?token={job.token}" if job.state == "done" else None}
        return data

    @app.post("/api/v1/schedules", status_code=201)
    async def create(body: ScheduleIn, request: Request):
        if not settings.scheduled_enabled:
            raise HTTPException(503, "Scheduled recording is turned off on this server")
        user, tier = await owner(request)
        url = await app.state.guard(request, body.url)
        # The same rules as recording right now: preset, plan, age gate, takedown list, egress filter.
        adult = app.state.precheck_live(url, body.preset, body.minutes, body.age_confirmed, tier)
        cap = tier.scheduled_max if settings.quotas_enabled else settings.max_scheduled_per_user
        if cap <= 0:
            raise HTTPException(403, "Scheduling recordings needs a Premium or Pro plan")
        if not await ratelimit.allow(app.state.redis, f"sched:{user.id}", 20, 3600):
            raise HTTPException(429, "Too many scheduling requests, try again later")

        now = time.time()
        if body.start_at is not None:
            if body.start_at.tzinfo is None:
                raise HTTPException(422, "Give the start time with its timezone (for example 2026-11-05T20:00:00+03:00)")
            start = body.start_at.timestamp()
        else:  # use the time the stream announces for itself
            try:
                info = await app.state.run_extract(app.state.extract_with_retries, url)
            except engine.EngineError as exc:
                raise HTTPException(422, f"Could not read that page to find its start time: {exc}") from exc
            if (await app.state.rules.get()).blocks_media(info.get("site"), info.get("id")):
                raise HTTPException(451, "This content is not available")
            if info.get("is_live") or info.get("live_status") == "is_live":
                raise HTTPException(409, "This stream is already live: use Record live stream instead")
            if info.get("live_status") != "is_upcoming" or not info.get("release_timestamp"):
                raise HTTPException(422, "This page does not announce a start time: choose one")
            start = float(info["release_timestamp"])
        if start < now + MIN_LEAD:
            raise HTTPException(422, "Choose a time in the future (to record a live stream right now, use Record live stream)")
        if start > now + settings.schedule_horizon_days * 86400:
            raise HTTPException(422, f"Recordings can be scheduled up to {settings.schedule_horizon_days} days ahead")

        row = await app.state.db.add_schedule(
            cap, settings.max_scheduled_total, user_id=user.id, url=url, start_at=start, minutes=body.minutes,
            preset=body.preset, adult=adult, age_confirmed=body.age_confirmed)
        if row == "user_cap":
            raise HTTPException(409, f"You already have {cap} recordings waiting: cancel one or wait for it to start")
        if row == "total_cap":
            raise HTTPException(503, "The server has too many scheduled recordings right now. Try again later.")
        await metrics.inc(app.state.redis, "hotload_scheduled_total", result="created")
        log(logger, "schedule created", schedule=row.id, user=user.id, start_in=int(start - now), minutes=body.minutes)
        return view(row)

    @app.get("/api/v1/schedules")
    async def listing(request: Request):
        user, _ = await owner(request)
        rows = await app.state.db.list_schedules(user.id)
        jobs = {r.id: await app.state.store.get(r.job_id) if r.job_id else None for r in rows}
        return [view(r, jobs[r.id]) for r in rows]

    @app.delete("/api/v1/schedules/{schedule_id}", status_code=204)
    async def remove(schedule_id: str, request: Request):
        user, _ = await owner(request)
        result = await app.state.db.cancel_schedule(user.id, schedule_id)
        if result is None:
            raise HTTPException(404, "Schedule not found")
        if result == "busy":
            raise HTTPException(409, "This recording has started or is starting: it cannot be cancelled now")
