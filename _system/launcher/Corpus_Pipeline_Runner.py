#!/usr/bin/env python3
"""Tkinter GUI launcher for all-staging Sonnet ingest + Opus review resolution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk

from launcher_cleanup import (
    PROJECT_ROOT,
    SYSTEM_ROOT,
    count_pending_child_documents,
    count_pending_supplements,
    count_review_pdfs,
    count_review_subfolder,
    count_staging_pdfs,
    count_technical_failure_pdfs,
    ensure_project_cwd,
    fetch_usd_to_aud_rate,
    make_run_id,
    pending_child_document_stats,
    request_stop,
    run_pre_launch_cleanup,
)

RUN_PREFIX = "all-staging-gui"
STOP_GRACE_SECONDS = 120
POLL_MS = 1000

TERMINAL_OK = {
    "done_clean",
    "done_with_deletions",
    "done_with_pending_children",
    "done_with_file_rejections",
    # legacy aliases during transition
    "completed",
    "done_with_review_items",
    "done_with_terminal_failures",
    "done_with_unresolved_files",
}
TERMINAL_STOPPED = {"stopped", "stopped_user", "interrupted"}
TERMINAL_FAIL = {
    "batch_stopped",
    "crashed",
    "failed_sonnet_unavailable",
    "failed_preconditions",
    "lock_busy",
    "failed",
    "failed_integrity",
}


def _format_cost_line(summary: dict | None, *, before_run: bool = False, estimating: bool = False) -> str:
    if before_run:
        return "Cost: waiting to start"
    if estimating or (summary and str(summary.get("status") or "") in {"estimating", ""} and not summary.get("items")):
        return "Cost: estimating..."

    if not summary:
        return "Cost: estimating..."

    rate = summary.get("usd_to_aud_rate")
    aud_label = "AUD unavailable"
    if rate and float(rate) > 0:
        aud_label = None

    est_total = summary.get("estimated_cost_so_far_usd")
    if est_total is None:
        est_total = summary.get("estimated_total_cost_usd")
    act_usd = summary.get("actual_cost_so_far_usd")
    if act_usd is None:
        act_usd = summary.get("total_cost_usd")

    in_tok = int(summary.get("actual_input_tokens_so_far") or 0)
    out_tok = int(summary.get("actual_output_tokens_so_far") or 0)
    has_tokens = in_tok > 0 or out_tok > 0

    est_aud = summary.get("estimated_cost_so_far_aud")
    if est_aud is None and est_total is not None and rate:
        est_aud = round(float(est_total) * float(rate), 2)
    act_aud = summary.get("actual_cost_so_far_aud")
    if act_aud is None and act_usd is not None and rate:
        act_aud = round(float(act_usd) * float(rate), 2)

    parts: list[str] = []

    if est_total is not None:
        if aud_label:
            parts.append(f"Estimated: ${float(est_total):.2f} USD / {aud_label}")
        elif est_aud is not None:
            parts.append(f"Estimated: ${float(est_total):.2f} USD / ${float(est_aud):.2f} AUD")
        else:
            parts.append(f"Estimated: ${float(est_total):.2f} USD")
    else:
        parts.append("Cost: estimating...")

    if act_usd is not None and (has_tokens or float(act_usd) > 0):
        if aud_label:
            parts.append(f"Actual: ${float(act_usd):.2f} USD / {aud_label}")
        elif act_aud is not None:
            parts.append(f"Actual: ${float(act_usd):.2f} USD / ${float(act_aud):.2f} AUD")
        else:
            parts.append(f"Actual: ${float(act_usd):.2f} USD")
    elif est_total is not None:
        parts.append("Actual: waiting for token usage...")

    if len(parts) == 1 and parts[0].startswith("Cost:"):
        return parts[0]
    return "\n".join(parts)


class CorpusPipelineRunnerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Research Corpus Pipeline")
        self.root.minsize(480, 320)

        self.run_id: str | None = None
        self.run_dir: Path | None = None
        self.proc: subprocess.Popen | None = None
        self.poll_after_id: str | None = None
        self.stop_requested = False
        self.stop_clicked_at: float | None = None
        self.force_killed = False
        self.usd_to_aud_rate: float | None = None
        self.last_processed = 0
        self.last_total = 0

        self.status_var = tk.StringVar(value="Waiting")
        self.processed_var = tk.StringVar(value="Processed: 0 / 0")
        self.review_res_var = tk.StringVar(value="Review resolution: -")
        self.staging_var = tk.StringVar(value="Raw staging candidates: 0")
        self.review_var = tk.StringVar(value="True technical/model failures: 0")
        self.review_queues_var = tk.StringVar(value="")
        self.db_summary_var = tk.StringVar(value="Complete papers: -")
        self.supplement_var = tk.StringVar(value="Pending child/support docs: 0")
        self.metacheck_var = tk.StringVar(value="Evidence checks: required")
        self.cost_var = tk.StringVar(value="Cost: waiting to start")
        self.message_var = tk.StringVar(value="")

        self._build_ui()
        self._refresh_counts()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 4}
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        btn_row = ttk.Frame(frame)
        btn_row.pack(anchor=tk.W, **pad)
        self.begin_btn = ttk.Button(btn_row, text="Begin", command=self._on_begin)
        self.begin_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.stop_btn = ttk.Button(btn_row, text="Stop", command=self._on_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)

        mode_row = ttk.LabelFrame(frame, text="Evidence checks")
        mode_row.pack(fill=tk.X, **pad)
        ttk.Label(mode_row, text="Structured evidence checks required for ratable research papers").pack(anchor=tk.W, padx=8, pady=4)

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X, **pad)

        ttk.Label(frame, textvariable=self.processed_var).pack(anchor=tk.W, **pad)
        ttk.Label(frame, textvariable=self.review_res_var).pack(anchor=tk.W, **pad)
        ttk.Label(frame, textvariable=self.staging_var).pack(anchor=tk.W, **pad)
        ttk.Label(frame, textvariable=self.status_var).pack(anchor=tk.W, **pad)
        ttk.Label(frame, textvariable=self.review_var).pack(anchor=tk.W, **pad)
        ttk.Label(frame, textvariable=self.db_summary_var).pack(anchor=tk.W, **pad)
        ttk.Label(frame, textvariable=self.supplement_var).pack(anchor=tk.W, **pad)
        ttk.Label(frame, textvariable=self.metacheck_var).pack(anchor=tk.W, **pad)
        ttk.Label(frame, textvariable=self.cost_var, justify=tk.LEFT).pack(anchor=tk.W, **pad)
        ttk.Label(frame, textvariable=self.message_var, wraplength=440).pack(anchor=tk.W, **pad)

    def _refresh_counts(self) -> None:
        from launcher_cleanup import count_staging_candidates, count_review_recovery_pending

        self.staging_var.set(
            f"Raw staging candidates: {count_staging_candidates():,}; "
            "Preflight duplicates deleted: 0; Unique to process: —; Processed unique: 0"
        )
        recovery_pending = count_review_recovery_pending()
        self.review_var.set(
            f"True technical/model failures: {count_technical_failure_pdfs() + recovery_pending}"
        )
        stats = pending_child_document_stats()
        self.supplement_var.set(
            f"Pending child/support docs: {stats['count']} (oldest {stats['oldest_days']} days)"
        )

    def _run_active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _url_ready(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                return int(resp.status) < 500
        except Exception:
            return False

    def _preflight_metacheck_mode(self) -> bool:
        self.status_var.set("Checking evidence services...")
        self.message_var.set("Starting/checking GROBID and evidence-check services...")
        self.root.update_idletasks()
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(SYSTEM_ROOT / "launcher" / "start_metacheck_services.ps1"),
                ],
                cwd=PROJECT_ROOT,
                timeout=180,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            self.status_var.set("Waiting")
            self.metacheck_var.set("Evidence checks: requirements not met")
            self.message_var.set(f"Requirements not met; evidence checks cannot proceed. ({exc})")
            return False
        grobid_ok = self._url_ready("http://127.0.0.1:8070/api/isalive")
        metacheck_ok = self._url_ready("http://127.0.0.1:2005/__docs__/")
        if result.returncode != 0 or not (grobid_ok and metacheck_ok):
            self.status_var.set("Waiting")
            self.metacheck_var.set("Evidence checks: requirements not met")
            detail = (result.stdout or result.stderr or "").strip().splitlines()[-1:] or [""]
            self.message_var.set(
                "Requirements not met; evidence checks cannot proceed. "
                f"GROBID={grobid_ok}; check service={metacheck_ok}. {detail[0]}"
            )
            return False
        self.metacheck_var.set("Evidence checks: ready")
        return True

    def _ai_profile(self) -> dict:
        profile_file = SYSTEM_ROOT / "corpus_profile.json"
        if not profile_file.exists():
            return {"profile": "public", "api_mode": "openrouter"}
        try:
            return json.loads(profile_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"profile": "public", "api_mode": "openrouter"}

    def _paid_ai_required(self) -> bool:
        api_mode = str(self._ai_profile().get("api_mode") or "openrouter").lower()
        return api_mode not in {"ollama_local", "local", "none"}

    def _ai_key_ready(self) -> tuple[bool, str]:
        api_mode = str(self._ai_profile().get("api_mode") or "openrouter").lower()
        key_by_mode = {
            "openrouter": ("OPENROUTER_API_KEY", "OpenRouter"),
            "deepseek": ("DEEPSEEK_API_KEY", "DeepSeek"),
            "openai": ("OPENAI_API_KEY", "OpenAI"),
            "anthropic": ("ANTHROPIC_API_KEY", "Anthropic"),
        }
        if api_mode in {"ollama_local", "local", "none"}:
            return True, "Local/free mode selected"
        env_name, label = key_by_mode.get(api_mode, ("OPENROUTER_API_KEY", "OpenRouter"))
        if os.environ.get(env_name, "").strip():
            return True, f"{label} key available"
        return False, f"{label} key is not set. Run SETUP.bat and choose an AI provider, or choose local/free mode."

    def _on_begin(self) -> None:
        key_ok, key_msg = self._ai_key_ready()
        if not key_ok:
            self.status_var.set("Failed")
            self.message_var.set(key_msg)
            return

        if not self._preflight_metacheck_mode():
            return

        self.stop_requested = False
        self.stop_clicked_at = None
        self.force_killed = False
        self.last_processed = 0
        self.last_total = 0

        rate, _source = fetch_usd_to_aud_rate()
        self.usd_to_aud_rate = rate

        self.begin_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.status_var.set("Estimating...")
        self.message_var.set("")
        self.progress["value"] = 0
        self.processed_var.set("Processed: 0 / 0")
        self.review_res_var.set("Review resolution: -")
        self.cost_var.set("Cost: estimating...")
        self.metacheck_var.set("Evidence checks: required")

        self.run_id = make_run_id(RUN_PREFIX)
        self.run_dir = SYSTEM_ROOT / "logs" / "runs" / self.run_id

        cleanup = run_pre_launch_cleanup(exclude_run_ids={self.run_id})
        if cleanup["logs"]["warnings"] or cleanup["backups"]["warnings"]:
            self.message_var.set("Pre-run cleanup had warnings (run continues).")

        paid_ai_required = self._paid_ai_required()
        api_mode = str(self._ai_profile().get("api_mode") or "openrouter").lower()
        cmd = [
            sys.executable,
            "pipeline/run_corpus_pipeline.py",
            "--workflow",
            "ingest",
            "--mode",
            "A",
            "--workflow-target",
            "all-staging",
            "--run-id",
            self.run_id,
            "--execute",
            "--allow-db-write",
            "--allow-pdf-copy",
        ]
        if paid_ai_required:
            cmd.append("--allow-paid-api")
            if api_mode == "anthropic":
                cmd.append("--allow-opus-resolution")
        else:
            cmd.append("--allow-local-ollama")
        env = os.environ.copy()
        env["METACHECK_MODE"] = "advanced"
        self.proc = subprocess.Popen(cmd, cwd=SYSTEM_ROOT, env=env)
        self._schedule_poll()

    def _on_stop(self) -> None:
        if not self._run_active():
            return
        self.stop_requested = True
        self.stop_clicked_at = time.monotonic()
        self.status_var.set("Stopping...")
        self.message_var.set("Stop requested. Finishing current safe checkpoint…")
        self.stop_btn.config(state=tk.DISABLED)
        if self.run_dir:
            request_stop(self.run_dir)

    def _on_close(self) -> None:
        if self._run_active():
            if not messagebox.askyesno(
                "Stop and close?",
                "A pipeline run is in progress.\n\nStop the run and close the window?",
            ):
                return
            self._on_stop()
            self.root.after(POLL_MS, self._wait_close_after_stop)
            return
        self.root.destroy()

    def _wait_close_after_stop(self) -> None:
        if self._run_active():
            self.root.after(POLL_MS, self._wait_close_after_stop)
            return
        self.root.destroy()

    def _schedule_poll(self) -> None:
        if self.poll_after_id:
            self.root.after_cancel(self.poll_after_id)
        self.poll_after_id = self.root.after(POLL_MS, self._poll_progress)

    def _read_live_summary(self) -> dict | None:
        if not self.run_dir:
            return None
        live_path = self.run_dir / "all_staging_ingest_live.json"
        if not live_path.exists():
            return None
        try:
            return json.loads(live_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _count_from_progress_jsonl(self) -> int:
        if not self.run_dir:
            return 0
        progress_path = self.run_dir / "all_staging_ingest_progress.jsonl"
        if not progress_path.exists():
            return 0
        done = 0
        try:
            with progress_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("event") == "paper_done":
                        done += 1
        except OSError:
            pass
        return done

    def _status_label(self, summary: dict | None, raw_status: str) -> str:
        if self.stop_requested and self._run_active():
            return "Stopping..."
        if self.stop_requested and not self._run_active():
            return "Stopped"

        phase = (summary or {}).get("phase") or ""
        phase_label = (summary or {}).get("phase_label") or ""
        rr_phase = (summary or {}).get("review_resolution_phase") or phase

        if summary and summary.get("run_completed_with_unresolved_files"):
            return "Done with unresolved files"
        if raw_status == "done_with_terminal_failures":
            return "Done with terminal failures"
        if raw_status == "done_clean":
            return "Done"
        if raw_status == "done_with_deletions":
            return "Done with deletions"
        if raw_status == "done_with_pending_children":
            return "Done with pending children"
        if raw_status == "done_with_file_rejections":
            return "Done with file rejections"
        if raw_status == "failed_integrity":
            return "Failed (integrity)"
        if raw_status == "stopped_user":
            return "Stopped"
        if raw_status == "done_with_review_items":
            return "Done with review items"
        if raw_status in TERMINAL_STOPPED:
            return "Stopped"
        if raw_status in TERMINAL_OK:
            return "Done"
        if raw_status in TERMINAL_FAIL:
            return "Failed"
        if phase_label:
            return phase_label
        if raw_status == "finalizing" or phase == "finalizing":
            return "Finalizing"
        if raw_status == "resolving_review" or rr_phase in {"pre_review", "post_review"}:
            if rr_phase == "post_review" or phase == "post_review":
                return "Resolving review after staging"
            return "Resolving review"
        if raw_status == "estimating":
            return "Estimating..."
        if raw_status == "running" or phase in {"staging", "processing_staging"}:
            return "Running staging"
        return "Waiting"

    def _update_review_progress(self, summary: dict | None, raw_status: str) -> None:
        if not summary:
            return
        rr_idx = int(summary.get("review_resolution_index") or 0)
        rr_total = int(summary.get("review_resolution_total") or 0)
        if raw_status == "resolving_review" and rr_total > 0:
            self.review_res_var.set(f"Review checked: {rr_idx} / {rr_total}")
        elif summary.get("pre_review_review_items_resolved") is not None and raw_status != "running":
            pre_r = int(summary.get("pre_review_review_items_resolved") or 0)
            pre_s = int(summary.get("pre_review_review_items_start") or 0)
            pre_rem = int(summary.get("pre_review_review_items_remaining_technical_failure") or 0)
            post_r = int(summary.get("post_review_review_items_resolved") or 0)
            post_s = int(summary.get("post_review_review_items_start") or 0)
            post_rem = int(summary.get("post_review_review_items_remaining_technical_failure") or 0)
            if post_s:
                self.review_res_var.set(f"Review checked post-run: {post_r} / {post_s}; Review remaining: {post_rem}")
            elif pre_s:
                self.review_res_var.set(f"Review checked pre-run: {pre_r} / {pre_s}; Review remaining: {pre_rem}")

    def _friendly_stage_label(self, stage: str) -> str:
        labels = {
            "start": "Starting file",
            "prescan_check": "Checking candidate",
            "ingest": "Ingesting paper",
            "llm_adjudication": "Reviewing blocked paper",
            "metacheck": "Evidence checks: preparing advanced checks",
            "metacheck_cached": "Evidence checks: using cached evidence",
            "metacheck_advanced_grobid_pdf_to_xml": "Evidence checks: running PDF structure extraction",
            "metacheck_advanced_metacheck_xml_checks": "Evidence checks: running structured checks",
            "metacheck_advanced_metacheck_done": "Evidence checks: advanced checks stored",
            "metacheck_not_applicable": "Evidence checks: not applicable",
            "first_pass_finalize": "Running corpus evaluation",
            "completion_check": "Checking evaluation completeness",
            "staging_cleanup": "Cleaning up staging",
        }
        return labels.get(stage, stage.replace("_", " ").title())

    def _format_metacheck_line(self, summary: dict | None) -> str:
        if not summary:
            return "Evidence checks: waiting"
        current_stage = str(summary.get("current_stage") or "")
        if current_stage.startswith("metacheck"):
            return self._friendly_stage_label(current_stage)
        adv = int(summary.get("metacheck_advanced_count") or 0)
        na = int(summary.get("metacheck_not_applicable_count") or 0)
        tech = int(summary.get("metacheck_technical_unavailable_count") or 0)
        failed = int(summary.get("metacheck_failed_count") or 0)
        if adv or na or tech or failed:
            return f"Evidence checks: complete {adv}; not applicable {na}; technical unavailable {tech}; failed {failed}"
        return "Evidence checks: required"

    def _maybe_force_kill(self) -> None:
        if not self.stop_requested or not self._run_active() or self.force_killed:
            return
        if self.stop_clicked_at is None:
            return
        if time.monotonic() - self.stop_clicked_at < STOP_GRACE_SECONDS:
            return
        try:
            self.proc.terminate()
        except OSError:
            pass
        if self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.force_killed = True
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.proc.poll() is not None:
            self.status_var.set("Stopped")
            self.begin_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
        self.message_var.set("Stop timeout — process force-terminated.")

    def _poll_progress(self) -> None:
        self._refresh_counts()

        processed = 0
        total = 0
        raw_status = "running"

        summary = self._read_live_summary()
        if summary:
            if summary.get("usd_to_aud_rate") is None and self.usd_to_aud_rate:
                summary["usd_to_aud_rate"] = self.usd_to_aud_rate
            total = int(summary.get("unique_candidates_to_process") or summary.get("starting_staging_count") or 0)
            processed = int(summary.get("processed_unique_candidates") or summary.get("processed_count") or len(summary.get("items", [])))
            raw_status = str(summary.get("status") or "running")
            self._update_review_progress(summary, raw_status)
            self.cost_var.set(
                _format_cost_line(
                    summary,
                    estimating=raw_status == "estimating" and processed == 0,
                )
            )
            if summary:
                self.db_summary_var.set(self._format_db_summary(summary))
                self.metacheck_var.set(self._format_metacheck_line(summary))
            pending = summary.get("pending_child_documents_remaining")
            if pending is None:
                pending = summary.get("pending_supplements_remaining")
            oldest = summary.get("oldest_pending_child_document_days")
            unresolved = int(summary.get("unresolved_staging_candidates") or 0)
            if unresolved and summary.get("run_completed_with_unresolved_files"):
                self.staging_var.set(f"Unresolved staging candidates: {unresolved}")
            if pending is not None:
                if oldest is not None:
                    self.supplement_var.set(f"Pending child documents: {pending} (oldest {oldest} days)")
                else:
                    self.supplement_var.set(f"Pending child documents: {pending}")
        else:
            processed = self._count_from_progress_jsonl()
            if self.stop_requested:
                self.cost_var.set(_format_cost_line(None, estimating=True))
            elif self.proc:
                self.cost_var.set("Cost: estimating...")

        self.last_processed = processed
        self.last_total = total

        phase = str((summary or {}).get("phase") or "")
        phase_label = str((summary or {}).get("phase_label") or self._status_label(summary, raw_status))
        phase_current = int((summary or {}).get("phase_current") or 0)
        phase_total = int((summary or {}).get("phase_total") or 0)
        current_file = str((summary or {}).get("current_file") or "")
        current_stage = str((summary or {}).get("current_stage") or "")
        raw_candidates = int((summary or {}).get("raw_staging_candidates_before_preflight") or 0)
        preflight_deleted = int((summary or {}).get("preflight_duplicates_deleted") or 0)
        unique_candidates = int((summary or {}).get("unique_candidates_to_process") or total)

        if summary:
            if raw_candidates or unique_candidates or preflight_deleted:
                self.staging_var.set(
                    f"Raw staging candidates: {raw_candidates:,}; "
                    f"Preflight duplicates deleted: {preflight_deleted:,}; "
                    f"Unique to process: {unique_candidates:,}; "
                    f"Processed unique: {processed:,}"
                )
            if phase == "preflight_dedupe":
                self.processed_var.set(f"Preflight dedupe: {phase_current:,} / {phase_total:,}")
            elif phase == "processing_staging":
                self.processed_var.set(f"Processed unique candidates: {processed:,} / {unique_candidates:,}")
            elif phase == "resolving_review":
                self.processed_var.set(f"Review/model checked: {phase_current:,} / {phase_total:,}")
            elif phase == "matching_pending_children":
                self.processed_var.set(f"Pending child/support match: {phase_current:,} / {phase_total:,}")
            elif phase in {"starting", "snapshotting_staging", "finalizing"}:
                self.processed_var.set(f"{phase_label}: waiting")
            elif phase in {"done", "stopped", "failed"}:
                self.processed_var.set(f"Processed unique candidates: {processed:,} / {unique_candidates or processed:,}")
            else:
                self.processed_var.set(f"Processed unique candidates: {processed:,} / {unique_candidates or '?'}")

            determinate_phases = {
                "preflight_dedupe",
                "processing_staging",
                "resolving_review",
                "matching_pending_children",
            }
            if phase in determinate_phases and phase_total > 0:
                self.progress["value"] = min(100, int(phase_current * 100 / phase_total))
            elif raw_status in TERMINAL_OK:
                self.progress["value"] = 100
            elif raw_status in TERMINAL_STOPPED or phase == "stopped":
                if phase_total > 0:
                    self.progress["value"] = min(99, int(phase_current * 100 / phase_total))
                else:
                    self.progress["value"] = 0
            else:
                self.progress["value"] = 0

            status_message = str(summary.get("status_message") or "")
            active_file_phases = {"preflight_dedupe", "processing_staging", "resolving_review"}
            if current_file and phase in active_file_phases:
                friendly_stage = self._friendly_stage_label(current_stage)
                detail = f"{friendly_stage}\nCurrent file: {current_file}" if current_stage else f"Current file: {current_file}"
                self.message_var.set(f"{status_message}\n{detail}" if status_message else detail)
            elif status_message:
                self.message_var.set(status_message)
        elif total > 0:
            self.processed_var.set(f"Processed unique candidates: {processed} / {total}")
            self.progress["value"] = min(100, int(processed * 100 / total))
        else:
            self.processed_var.set("Processed unique candidates: 0 / ?")

        self.status_var.set(self._status_label(summary, raw_status))

        proc_done = self.proc is None or self.proc.poll() is not None
        if not proc_done:
            self._maybe_force_kill()
            self._schedule_poll()
            return

        self._finish_run(summary, raw_status, processed, total)

    def _format_db_summary(self, summary: dict | None) -> str:
        if not summary:
            return "Complete papers: -"
        before = summary.get("clean_complete_papers_before")
        after = summary.get("clean_complete_papers_after")
        new = summary.get("new_complete_papers_added")
        if before is None or after is None:
            return "Complete papers: -"
        new_part = f" (+{new})" if new is not None and int(new) > 0 else ""
        return f"Complete papers: {int(before):,} -> {int(after):,}{new_part}"

    def _finish_run(self, summary: dict | None, raw_status: str, processed: int, total: int) -> None:
        tech_fail = count_technical_failure_pdfs()
        staging_left = count_staging_pdfs()
        review_all = count_review_pdfs()

        if summary:
            self.cost_var.set(_format_cost_line(summary))
            self.db_summary_var.set(self._format_db_summary(summary))
            self.metacheck_var.set(self._format_metacheck_line(summary))

        label = self._status_label(summary, raw_status)
        self.status_var.set(label)

        new_papers = int(summary.get("new_complete_papers_added") or 0) if summary else 0

        if label in {
            "Done",
            "Done with review items",
            "Done with terminal failures",
            "Done with unresolved files",
            "Done with deletions",
            "Done with pending children",
            "Done with file rejections",
        } and self.proc and self.proc.returncode == 0:
            extra = ""
            if label == "Done with review items":
                routed = int(summary.get("routed_review_count") or 0) if summary else 0
                extra = f" {routed} item(s) routed to review for metadata recovery."
            elif label == "Done with terminal failures":
                tf = int(summary.get("technical_failure_count") or 0) if summary else 0
                junk = int(summary.get("deleted_junk_count") or 0) if summary else 0
                extra = f" Terminal technical/model failures: {tf}; deleted junk: {junk}."
            elif label == "Done with deletions":
                rej = int(summary.get("file_rejections_count") or 0) if summary else 0
                extra = f" File rejections/deletions: {rej}."
            elif label == "Done with pending children":
                pending = int(summary.get("pending_child_documents_remaining") or 0) if summary else 0
                extra = f" Pending child/support docs: {pending}."
            review_dust = int(summary.get("review_dust_remaining") or 0) if summary else 0
            staging_left = int(summary.get("final_staging_count") or count_staging_pdfs()) if summary else count_staging_pdfs()
            unresolved = int(summary.get("unresolved_staging_candidates") or 0) if summary else 0
            if label == "Done with unresolved files":
                extra = f" Unresolved staging candidates: {unresolved}. Run completed with unresolved files: YES."
            self.message_var.set(
                f"{label}. Processed {processed} / {total or processed}. "
                f"New papers added this run: {new_papers}. "
                f"Research: {int(summary.get('research_papers_added') or 0)}, "
                f"Non-ratable/reference: {int(summary.get('non_ratable_reference_added') or 0)}. "
                f"Recovered (research/ref): {int(summary.get('recovered_research_papers') or 0)}/{int(summary.get('recovered_reference_docs') or 0)}. "
                f"Model recovery pending: {int(summary.get('model_recovery_required') or summary.get('model_recovery_required_remaining') or 0)}. "
                f"Pending child docs: {int(summary.get('pending_child_remaining') or summary.get('pending_child_documents_remaining') or 0)} "
                f"(corrupt deleted: {int(summary.get('pending_child_corrupt_deleted') or 0)}). "
                f"Evidence checks complete/not-applicable/technical unavailable/failed: "
                f"{int(summary.get('metacheck_advanced_count') or 0)}/"
                f"{int(summary.get('metacheck_not_applicable_count') or 0)}/"
                f"{int(summary.get('metacheck_technical_unavailable_count') or 0)}/"
                f"{int(summary.get('metacheck_failed_count') or 0)}.{extra} "
                f"Staging remaining: {staging_left}. Review dust remaining: {review_dust}."
            )
            if total > 0:
                self.progress["value"] = 100
        elif label == "Stopped" or self.stop_requested or raw_status in TERMINAL_STOPPED:
            self.status_var.set("Stopped")
            self.message_var.set(
                f"Stopped. Processed {processed} / {total or processed}. "
                f"Review folder has {review_all} files."
            )
        else:
            exit_code = self.proc.returncode if self.proc else "?"
            detail = summary.get("error") if summary else ""
            msg = f"Failed. Check logs: logs\\runs\\{self.run_id}\\"
            if detail:
                msg += f" ({detail})"
            elif exit_code not in (0, None, "?"):
                msg += f" (exit {exit_code})"
            msg += f"\nProcessed {processed} / {total or processed}. Review folder has {review_all} files."
            self.message_var.set(msg)
            self.status_var.set("Failed")

        self.begin_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.stop_requested = False
        self.stop_clicked_at = None
        self.poll_after_id = None


def main() -> None:
    ensure_project_cwd()
    root = tk.Tk()
    CorpusPipelineRunnerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
