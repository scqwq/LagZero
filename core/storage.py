"""
core/storage.py — SQLite persistence via SQLAlchemy.

Tables:
  lag_events     — one row per confirmed lag event
  lag_snapshots  — one row per snapshot (linked to an event)
  process_logs   — top processes at peak of each event

No external server needed; the .db file lives in the data/ folder.
"""
import json
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column, Integer, Float, String, DateTime, Text, ForeignKey,
    create_engine, delete, select, text
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship
from sqlalchemy.pool import NullPool

from core.models import LagEvent, LagSnapshot

# ---------------------------------------------------------------------------
# Database path
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent.parent / "data" / "lag_history.db"


def _ensure_data_dir():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _build_engine(db_path: Path):
    """
    Engine shared by the UI thread and the persistence worker threads.

    SQLite's default journal mode locks the whole database file for every
    writer and every reader: a snapshot save from the UI thread while a
    background read was in flight would block on the file lock, which showed
    up as the UI stuttering exactly when events landed. WAL mode lets one
    writer proceed alongside concurrent readers, busy_timeout waits out short
    contention instead of raising, and NullPool gives every Session its own
    fresh connection so a pooled connection never hops between threads.
    """
    engine = create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        poolclass=NullPool,
        connect_args={
            "check_same_thread": False,
            "timeout": 10,
        },
    )
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
        conn.exec_driver_sql("PRAGMA busy_timeout=10000")
    return engine


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class LagEventRow(Base):
    __tablename__ = "lag_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=True)
    peak_composite_score = Column(Float, default=0.0)
    cause = Column(Text, default="")
    cause_code = Column(String(64), default="UNKNOWN")
    category = Column(String(64), default="")
    scope = Column(String(64), default="")
    duration_seconds = Column(Float, default=0.0)
    # Frame/response timings for detector-sourced events. Stored separately from
    # `cause` so a reloaded report can still show what the player saw, not just
    # the system-side root cause.
    frame_summary = Column(Text, default="")

    snapshots = relationship("LagSnapshotRow", back_populates="event", cascade="all, delete-orphan")


class LagSnapshotRow(Base):
    __tablename__ = "lag_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("lag_events.id"), nullable=True)
    captured_at = Column(DateTime, nullable=False)
    peak_cpu = Column(Float, default=0.0)
    peak_ram = Column(Float, default=0.0)
    peak_responsiveness_ms = Column(Float, default=0.0)
    top_processes_json = Column(Text, default="[]")    # JSON list of {name, pid, cpu, mem}
    process_groups_json = Column(Text, default="[]")   # JSON list of aggregated process groups
    pre_lag_summary_json = Column(Text, default="[]")  # JSON list of {ts, cpu, ram, resp}
    target_process_json = Column(Text, default="{}")
    gpu_memory_json = Column(Text, default="{}")

    event = relationship("LagEventRow", back_populates="snapshots")


# ---------------------------------------------------------------------------
# Storage class
# ---------------------------------------------------------------------------

class Storage:

    def __init__(self, db_path: Path = DB_PATH):
        _ensure_data_dir()
        self._engine = _build_engine(db_path)
        Base.metadata.create_all(self._engine)
        self._ensure_schema()

    def _ensure_schema(self):
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(lag_events)").fetchall()
            }
            if "category" not in existing:
                conn.exec_driver_sql("ALTER TABLE lag_events ADD COLUMN category VARCHAR(64) DEFAULT ''")
            if "scope" not in existing:
                conn.exec_driver_sql("ALTER TABLE lag_events ADD COLUMN scope VARCHAR(64) DEFAULT ''")
            if "frame_summary" not in existing:
                conn.exec_driver_sql("ALTER TABLE lag_events ADD COLUMN frame_summary TEXT DEFAULT ''")
            snapshot_existing = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(lag_snapshots)").fetchall()
            }
            if "target_process_json" not in snapshot_existing:
                conn.exec_driver_sql("ALTER TABLE lag_snapshots ADD COLUMN target_process_json TEXT DEFAULT '{}'")
            if "gpu_memory_json" not in snapshot_existing:
                conn.exec_driver_sql("ALTER TABLE lag_snapshots ADD COLUMN gpu_memory_json TEXT DEFAULT '{}'")
            if "process_groups_json" not in snapshot_existing:
                conn.exec_driver_sql("ALTER TABLE lag_snapshots ADD COLUMN process_groups_json TEXT DEFAULT '[]'")

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_event(self, event: LagEvent) -> int:
        """Inserts or updates a LagEvent. Returns the row id."""
        with Session(self._engine) as session:
            if event.id:
                row = session.get(LagEventRow, event.id)
                if row is None:
                    row = LagEventRow()
                    session.add(row)
            else:
                row = LagEventRow()
                session.add(row)

            row.started_at = event.started_at
            row.ended_at = event.ended_at
            row.peak_composite_score = event.peak_composite_score
            row.cause = event.cause
            row.cause_code = event.cause_code
            row.category = event.category
            row.scope = event.scope
            row.duration_seconds = event.duration_seconds
            row.frame_summary = event.frame_summary

            session.commit()
            session.refresh(row)
            return row.id

    def save_snapshot(self, snapshot: LagSnapshot) -> int:
        """Persists a LagSnapshot. Returns row id."""
        with Session(self._engine) as session:
            procs_json = json.dumps([
                {
                    "name": p.name,
                    "pid": p.pid,
                    "cpu_percent": round(p.cpu_percent, 1),
                    "memory_mb": round(p.memory_mb, 1),
                    "cpu_machine_share": round(p.cpu_machine_share, 1),
                }
                for p in snapshot.top_processes
            ])
            groups_json = json.dumps([
                {
                    "name": group.name,
                    "process_count": group.process_count,
                    "cpu_machine_share": round(group.cpu_machine_share, 1),
                    "memory_mb": round(group.memory_mb, 1),
                }
                for group in snapshot.process_groups
            ])

            pre_json = json.dumps([
                {
                    "ts": s.timestamp.isoformat(),
                    "cpu": round(s.cpu_percent, 1),
                    "ram": round(s.ram_percent, 1),
                    "ram_available_gb": round(s.ram_available_mb / 1024, 2),
                    "ram_total_gb": round(s.ram_total_mb / 1024, 2),
                    "resp": round(s.responsiveness_ms, 1),
                    "target_name": s.target_process.name if s.target_process else "",
                    "target_pid": s.target_process.pid if s.target_process else 0,
                    "target_cpu": round(s.target_process.cpu_percent, 1) if s.target_process else 0.0,
                    "target_mem": round(s.target_process.memory_mb, 1) if s.target_process else 0.0,
                    "target_private_mem": round(s.target_process.private_memory_mb, 1) if s.target_process else 0.0,
                    "gpu_local_mb": round(s.gpu_memory.local_usage_mb, 1) if s.gpu_memory else 0.0,
                    "gpu_local_budget_mb": round(s.gpu_memory.local_budget_mb, 1) if s.gpu_memory else 0.0,
                }
                for s in snapshot.pre_lag_samples
            ])
            target_json = json.dumps(
                {
                    "name": snapshot.peak_sample.target_process.name,
                    "pid": snapshot.peak_sample.target_process.pid,
                    "cpu_percent": round(snapshot.peak_sample.target_process.cpu_percent, 1),
                    "memory_mb": round(snapshot.peak_sample.target_process.memory_mb, 1),
                    "private_memory_mb": round(snapshot.peak_sample.target_process.private_memory_mb, 1),
                    "read_kb_s": round(snapshot.peak_sample.target_process.read_kb_s, 1),
                    "write_kb_s": round(snapshot.peak_sample.target_process.write_kb_s, 1),
                    "thread_count": snapshot.peak_sample.target_process.thread_count,
                    "cpu_machine_share": round(snapshot.peak_sample.target_process.cpu_machine_share, 1),
                }
                if snapshot.peak_sample.target_process
                else {}
            )
            gpu_json = json.dumps(
                {
                    "local_usage_mb": round(snapshot.peak_sample.gpu_memory.local_usage_mb, 1),
                    "local_budget_mb": round(snapshot.peak_sample.gpu_memory.local_budget_mb, 1),
                    "shared_usage_mb": round(snapshot.peak_sample.gpu_memory.shared_usage_mb, 1),
                    "shared_budget_mb": round(snapshot.peak_sample.gpu_memory.shared_budget_mb, 1),
                    "local_usage_ratio": round(snapshot.peak_sample.gpu_memory.local_usage_ratio, 4),
                    "shared_usage_ratio": round(snapshot.peak_sample.gpu_memory.shared_usage_ratio, 4),
                }
                if snapshot.peak_sample.gpu_memory
                else {}
            )

            row = LagSnapshotRow(
                event_id=snapshot.event_id,
                captured_at=snapshot.captured_at,
                peak_cpu=snapshot.peak_cpu,
                peak_ram=snapshot.peak_ram,
                peak_responsiveness_ms=snapshot.peak_responsiveness_ms,
                top_processes_json=procs_json,
                process_groups_json=groups_json,
                pre_lag_summary_json=pre_json,
                target_process_json=target_json,
                gpu_memory_json=gpu_json,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.id

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_recent_events(
        self,
        limit: int = 100,
        offset: int = 0,
        event_type: str | None = None,
    ) -> list[LagEvent]:
        with Session(self._engine) as session:
            stmt = select(LagEventRow)
            if event_type == "pressure":
                stmt = stmt.where(LagEventRow.category == "RESOURCE_PRESSURE_RISK")
            elif event_type == "stutter":
                stmt = stmt.where(LagEventRow.category != "RESOURCE_PRESSURE_RISK")
            stmt = (
                stmt
                .order_by(LagEventRow.started_at.desc())
                .offset(offset)
                .limit(limit)
            )
            rows = session.scalars(stmt).all()
            return [self._row_to_event(r) for r in rows]

    def get_snapshot_for_event(self, event_id: int) -> LagSnapshot | None:
        with Session(self._engine) as session:
            stmt = (
                select(LagSnapshotRow)
                .where(LagSnapshotRow.event_id == event_id)
                .order_by(LagSnapshotRow.captured_at.desc())
                .limit(1)
            )
            row = session.scalars(stmt).first()
            if row is None:
                return None
            return self._row_to_snapshot(row)

    def event_count(self, event_type: str | None = None) -> int:
        with Session(self._engine) as session:
            from sqlalchemy import func
            stmt = select(func.count()).select_from(LagEventRow)
            if event_type == "pressure":
                stmt = stmt.where(LagEventRow.category == "RESOURCE_PRESSURE_RISK")
            elif event_type == "stutter":
                stmt = stmt.where(LagEventRow.category != "RESOURCE_PRESSURE_RISK")
            return session.scalar(stmt) or 0

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_event(self, event_id: int | None) -> bool:
        if not event_id:
            return False
        with Session(self._engine) as session:
            row = session.get(LagEventRow, event_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def delete_all_events(self, event_type: str | None = None) -> int:
        with Session(self._engine) as session:
            from sqlalchemy import func

            count_stmt = select(func.count()).select_from(LagEventRow)
            if event_type == "pressure":
                count_stmt = count_stmt.where(LagEventRow.category == "RESOURCE_PRESSURE_RISK")
            elif event_type == "stutter":
                count_stmt = count_stmt.where(LagEventRow.category != "RESOURCE_PRESSURE_RISK")
            count = session.scalar(count_stmt) or 0
            if count <= 0:
                return 0
            del_stmt = delete(LagEventRow)
            if event_type == "pressure":
                del_stmt = del_stmt.where(LagEventRow.category == "RESOURCE_PRESSURE_RISK")
            elif event_type == "stutter":
                del_stmt = del_stmt.where(LagEventRow.category != "RESOURCE_PRESSURE_RISK")
            session.execute(del_stmt)
            session.commit()
            return int(count)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_event(row: LagEventRow) -> LagEvent:
        return LagEvent(
            id=row.id,
            started_at=row.started_at,
            ended_at=row.ended_at,
            peak_composite_score=row.peak_composite_score,
            cause=row.cause,
            cause_code=row.cause_code,
            category=row.category or "",
            scope=row.scope or "",
            duration_seconds=row.duration_seconds,
            frame_summary=row.frame_summary or "",
        )

    @staticmethod
    def _row_to_snapshot(row: LagSnapshotRow) -> LagSnapshot:
        from core.models import GpuMemorySnapshot, ProcessGroupSample, ProcessSample, SystemSample, TargetProcessMetrics
        procs = [
            ProcessSample(
                pid=p["pid"],
                name=p["name"],
                cpu_percent=p["cpu_percent"],
                memory_mb=p["memory_mb"],
                cpu_machine_share=p.get("cpu_machine_share", 0.0),
            )
            for p in json.loads(row.top_processes_json)
        ]
        process_groups = [
            ProcessGroupSample(
                name=group["name"],
                process_count=group["process_count"],
                cpu_machine_share=group["cpu_machine_share"],
                memory_mb=group["memory_mb"],
            )
            for group in json.loads(row.process_groups_json or "[]")
        ]
        # Reconstruct minimal SystemSamples for the timeline chart
        pre_samples = []
        for s in json.loads(row.pre_lag_summary_json):
            target_process = None
            if s.get("target_pid"):
                target_process = TargetProcessMetrics(
                    pid=s["target_pid"],
                    name=s.get("target_name", ""),
                    cpu_percent=s.get("target_cpu", 0.0),
                    memory_mb=s.get("target_mem", 0.0),
                    private_memory_mb=s.get("target_private_mem", s.get("target_mem", 0.0)),
                )
            gpu_memory = None
            if s.get("gpu_local_budget_mb", 0.0) > 0:
                local_usage = s.get("gpu_local_mb", 0.0)
                local_budget = s.get("gpu_local_budget_mb", 0.0)
                gpu_memory = GpuMemorySnapshot(
                    local_usage_mb=local_usage,
                    local_budget_mb=local_budget,
                    local_usage_ratio=(local_usage / local_budget) if local_budget > 0 else 0.0,
                )
            pre_samples.append(SystemSample(
                timestamp=datetime.fromisoformat(s["ts"]),
                cpu_percent=s["cpu"],
                cpu_per_core=[],
                ram_percent=s["ram"],
                ram_used_mb=0,
                ram_total_mb=s.get("ram_total_gb", 0.0) * 1024,
                swap_percent=0,
                ram_available_mb=s.get("ram_available_gb", 0.0) * 1024,
                responsiveness_ms=s["resp"],
                top_processes=[],
                target_process=target_process,
                gpu_memory=gpu_memory,
            ))
        target_payload = json.loads(row.target_process_json or "{}")
        target_process = None
        if target_payload.get("pid"):
            target_process = TargetProcessMetrics(
                pid=target_payload["pid"],
                name=target_payload.get("name", ""),
                cpu_percent=target_payload.get("cpu_percent", 0.0),
                memory_mb=target_payload.get("memory_mb", 0.0),
                private_memory_mb=target_payload.get("private_memory_mb", target_payload.get("memory_mb", 0.0)),
                read_kb_s=target_payload.get("read_kb_s", 0.0),
                write_kb_s=target_payload.get("write_kb_s", 0.0),
                thread_count=target_payload.get("thread_count", 0),
                cpu_machine_share=target_payload.get("cpu_machine_share", 0.0),
            )
        gpu_payload = json.loads(row.gpu_memory_json or "{}")
        gpu_memory = None
        if gpu_payload.get("local_budget_mb", 0.0) > 0:
            gpu_memory = GpuMemorySnapshot(
                local_usage_mb=gpu_payload.get("local_usage_mb", 0.0),
                local_budget_mb=gpu_payload.get("local_budget_mb", 0.0),
                shared_usage_mb=gpu_payload.get("shared_usage_mb", 0.0),
                shared_budget_mb=gpu_payload.get("shared_budget_mb", 0.0),
                local_usage_ratio=gpu_payload.get("local_usage_ratio", 0.0),
                shared_usage_ratio=gpu_payload.get("shared_usage_ratio", 0.0),
            )

        return LagSnapshot(
            id=row.id,
            event_id=row.event_id,
            captured_at=row.captured_at,
            pre_lag_samples=pre_samples,
            peak_sample=pre_samples[-1] if pre_samples else SystemSample(
                timestamp=row.captured_at,
                cpu_percent=row.peak_cpu,
                cpu_per_core=[],
                ram_percent=row.peak_ram,
                ram_used_mb=0,
                ram_total_mb=0,
                swap_percent=0,
                responsiveness_ms=row.peak_responsiveness_ms,
                top_processes=procs,
                process_groups=process_groups,
                target_process=target_process,
                gpu_memory=gpu_memory,
            ),
            top_processes=procs,
            process_groups=process_groups,
            peak_cpu=row.peak_cpu,
            peak_ram=row.peak_ram,
            peak_responsiveness_ms=row.peak_responsiveness_ms,
        )
