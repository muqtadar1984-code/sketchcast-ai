"""Write a validated Plan into Supabase. The only part that touches the network.

DESIGN RULES, all of them learned the hard way elsewhere in this codebase:

* NOTHING is written unless parse.py returned zero errors. A half-imported school
  is worse than an unimported one, because the school cannot tell which half.
* IDEMPOTENT. Re-running matches on the human keys the school gave us — school by
  slug, staff by email, class by (school, name), student by username, link by
  (parent, child) — and updates instead of duplicating. Onboarding calls always
  end with "one more teacher joined", and that must be a re-run, not a repair job.
* DEPENDENCY ORDER. School, staff, classes, scopes, timetable, students,
  enrolments, parents, links. Each step reports what it did before the next
  starts, so an interruption leaves a readable trail.
* NO EMAIL IS EVER SENT. Adults get an invite ROW and a link we hand to the
  school; students get an account and a temporary password. Nobody is contacted
  by us. The deck's promise is "invited, not registered", and a bulk mailout to a
  school's whole staff list on the day of setup is exactly the wrong first
  impression.
* ORPHAN CLEANUP. If a profile write fails after an auth user was created, the
  auth user is deleted again — the same guard src/app/api/children/route.ts uses,
  for the same reason: never hand out credentials for an account that is not
  properly attached.
"""

from __future__ import annotations

import random
import secrets
import string
from dataclasses import dataclass, field
from typing import Any

from .parse import STUDENT_EMAIL_DOMAIN, Plan

# Mirrors src/utils/temp-password.ts. The Supabase project's password policy can
# demand one character from EACH of lower/upper/digit/symbol; this format
# satisfies all four by construction. Do NOT swap the hyphen out — it is in
# GoTrue's accepted symbol set and the rest of the words are letters only.
_WORDS = [
    "amber", "brook", "cedar", "delta", "ember", "fable", "grove", "harbor",
    "indigo", "jasper", "kite", "lantern", "meadow", "nectar", "opal", "pebble",
    "quartz", "ripple", "summit", "thicket", "umber", "violet", "willow", "zephyr",
]
_DIGITS = "23456789"


def temp_password() -> str:
    words = [_WORDS[secrets.randbelow(len(_WORDS))] for _ in range(3)]
    words[0] = words[0].capitalize()
    return "-".join(words) + secrets.choice(_DIGITS) + secrets.choice(_DIGITS)


def student_email(username: str) -> str:
    return f"{username.strip().lower()}@{STUDENT_EMAIL_DOMAIN}"


@dataclass
class Result:
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    # Handed to the school. Shown once — students cannot be told their password later.
    student_credentials: list[dict] = field(default_factory=list)
    invite_links: list[dict] = field(default_factory=list)
    school_id: str | None = None

    def bump(self, bucket: dict[str, int], what: str, n: int = 1) -> None:
        bucket[what] = bucket.get(what, 0) + n

    def render(self) -> str:
        def fmt(d: dict[str, int]) -> str:
            return ", ".join(f"{k} {v}" for k, v in sorted(d.items())) or "nothing"

        out = [f"created:  {fmt(self.created)}", f"updated:  {fmt(self.updated)}",
               f"skipped:  {fmt(self.skipped)}"]
        if self.failures:
            out.append(f"FAILURES ({len(self.failures)}):")
            out += [f"  - {f}" for f in self.failures]
        return "\n".join(out)


def _one(res) -> dict | None:
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def apply_plan(sb, plan: Plan, *, site_url: str = "https://school.sketchcast.app",
               invite_days: int = 30) -> Result:
    """Write the plan. Caller must have checked plan.ok."""
    if not plan.ok:
        raise ValueError("refusing to apply a plan with errors — fix the workbook first")
    r = Result()

    # ── school ───────────────────────────────────────────────────────────────
    s = plan.school
    config: dict[str, Any] = {}
    tt = {k: v for k, v in {
        "start": s.get("start"), "end": s.get("end"), "days": s.get("days"),
        "periodMinutes": s.get("period_minutes"),
        "maxPerTeacherPerDay": s.get("max_per_teacher_per_day"),
        "periods": [{"label": p["label"], "time": p["time"]} for p in plan.periods] or None,
        "breaks": [{"afterPeriod": b["afterPeriod"], "label": b["label"],
                    "time": b["time"], "minutes": b["minutes"]} for b in plan.breaks] or None,
    }.items() if v is not None}
    if tt:
        config["timetable"] = tt
    for key, val in (("timetable_enabled", s.get("timetable_enabled")),
                     ("calendar", s.get("calendar")),
                     ("school_analytics", s.get("school_analytics")),
                     ("school_assistant", s.get("school_assistant"))):
        if val is not None:
            config[key] = val

    existing = _one(sb.table("schools").select("id,config").eq("slug", s["slug"]).limit(1).execute())
    if existing:
        school_id = existing["id"]
        # Merge, never replace: a school edited in the console keeps those edits.
        merged = {**(existing.get("config") or {}), **config}
        sb.table("schools").update({
            "name": s["name"], "display_name": s.get("display_name"), "config": merged,
        }).eq("id", school_id).execute()
        r.bump(r.updated, "school")
    else:
        row = _one(sb.table("schools").insert({
            "name": s["name"], "display_name": s.get("display_name"),
            "slug": s["slug"], "config": config, "status": "active",
        }).select("id").execute())
        school_id = row["id"] if row else None
        r.bump(r.created, "school")
    r.school_id = school_id

    # ── staff: an invite row each, never an account ──────────────────────────
    staff_ids: dict[str, str] = {}
    for st in plan.staff:
        email = st["email"]
        prof = _one(
            sb.table("profiles").select("id,school_id,role")
            .eq("id", _user_id_for_email(sb, email) or "00000000-0000-0000-0000-000000000000")
            .limit(1).execute()
        ) if _user_id_for_email(sb, email) else None
        if prof:
            # Already has an account — attach them to the school and set the role.
            patch = {"school_id": school_id, "role": st["role"], "full_name": st["full_name"]}
            if st.get("locale"):
                patch["ui_locale"] = st["locale"]
            sb.table("profiles").update(patch).eq("id", prof["id"]).execute()
            staff_ids[email] = prof["id"]
            r.bump(r.updated, "staff")
            continue
        have = _one(sb.table("invites").select("id,token")
                    .eq("email", email).eq("school_id", school_id)
                    .is_("accepted_at", "null").limit(1).execute())
        if have:
            token = have["token"]
            r.bump(r.skipped, "staff invite (already open)")
        else:
            token = secrets.token_urlsafe(32)
            try:
                sb.table("invites").insert({
                    "email": email, "role": st["role"], "school_id": school_id,
                    "token": token,
                    "expires_at": _in_days(invite_days),
                }).execute()
                r.bump(r.created, "staff invite")
            except Exception as exc:  # noqa: BLE001
                r.failures.append(f"staff {email}: {exc}")
                continue
        r.invite_links.append({"name": st["full_name"], "email": email, "role": st["role"],
                               "link": f"{site_url}/invite/{token}"})

    # ── classes ──────────────────────────────────────────────────────────────
    class_ids: dict[str, str] = {}
    for c in plan.classes:
        teacher_id = staff_ids.get(c["teacher_email"])
        if not teacher_id:
            # classes.teacher_id is NOT NULL, so a class whose teacher has not
            # accepted their invite yet cannot be created. Say so plainly rather
            # than inventing an owner.
            r.failures.append(
                f"class {c['name']!r}: its class teacher {c['teacher_email']} has no account yet — "
                f"re-run this import after they accept their invite")
            r.bump(r.skipped, "class")
            continue
        have = _one(sb.table("classes").select("id")
                    .eq("school_id", school_id).eq("name", c["name"]).limit(1).execute())
        if have:
            sb.table("classes").update({"grade": c["grade"], "teacher_id": teacher_id}) \
                .eq("id", have["id"]).execute()
            class_ids[c["name"]] = have["id"]
            r.bump(r.updated, "class")
        else:
            row = _one(sb.table("classes").insert({
                "name": c["name"], "grade": c["grade"], "teacher_id": teacher_id,
                "school_id": school_id, "join_code": _join_code(),
            }).select("id").execute())
            if row:
                class_ids[c["name"]] = row["id"]
                r.bump(r.created, "class")

    # ── coordinator scope ────────────────────────────────────────────────────
    for sc in plan.scopes:
        cid = staff_ids.get(sc["email"])
        if not cid:
            r.bump(r.skipped, "coordinator grant (no account yet)")
            continue
        q = (sb.table("coordinator_scope").select("id")
             .eq("coordinator_id", cid).eq("school_id", school_id).eq("grade", sc["grade"]))
        q = q.is_("subject", "null") if sc["subject"] is None else q.eq("subject", sc["subject"])
        if _one(q.limit(1).execute()):
            r.bump(r.skipped, "coordinator grant")
            continue
        sb.table("coordinator_scope").insert({
            "coordinator_id": cid, "school_id": school_id,
            "grade": sc["grade"], "subject": sc["subject"],
        }).execute()
        r.bump(r.created, "coordinator grant")

    # ── timetable ────────────────────────────────────────────────────────────
    for sl in plan.slots:
        cid = class_ids.get(sl["class_name"])
        if not cid:
            r.bump(r.skipped, "timetable slot (class not created)")
            continue
        payload = {
            "school_id": school_id, "class_id": cid, "day": sl["day"], "period": sl["period"],
            "subject": sl["subject"], "teacher_id": staff_ids.get(sl["teacher_email"]),
            "room": sl["room"] or None,
        }
        have = _one(sb.table("timetable_slots").select("id")
                    .eq("class_id", cid).eq("day", sl["day"]).eq("period", sl["period"])
                    .limit(1).execute())
        if have:
            sb.table("timetable_slots").update(payload).eq("id", have["id"]).execute()
            r.bump(r.updated, "timetable slot")
        else:
            sb.table("timetable_slots").insert(payload).execute()
            r.bump(r.created, "timetable slot")

    # ── students ─────────────────────────────────────────────────────────────
    student_ids: dict[str, str] = {}
    for stu in plan.students:
        user = stu["username"]
        have = _one(sb.table("profiles").select("id").eq("username", user).limit(1).execute())
        if have:
            sb.table("profiles").update({
                "full_name": stu["full_name"], "school_id": school_id, "role": "student",
                **({"ui_locale": stu["locale"]} if stu.get("locale") else {}),
            }).eq("id", have["id"]).execute()
            student_ids[user] = have["id"]
            r.bump(r.updated, "student")
        else:
            pw = temp_password()
            try:
                made = sb.auth.admin.create_user({
                    "email": student_email(user), "password": pw, "email_confirm": True,
                    "user_metadata": {"full_name": stu["full_name"], "role": "student"},
                })
            except Exception as exc:  # noqa: BLE001
                r.failures.append(f"student {user}: {exc}")
                continue
            sid = getattr(getattr(made, "user", None), "id", None)
            if not sid:
                r.failures.append(f"student {user}: auth user was not returned")
                continue
            try:
                # handle_new_user() has already made the profile row — fill the rest.
                sb.table("profiles").update({
                    "username": user, "full_name": stu["full_name"], "school_id": school_id,
                    "role": "student", "must_reset_password": True,
                    **({"ui_locale": stu["locale"]} if stu.get("locale") else {}),
                }).eq("id", sid).execute()
            except Exception as exc:  # noqa: BLE001
                # Never leave an account we cannot attach — and never hand out its
                # password. Same guard as api/children/route.ts.
                r.failures.append(f"student {user}: profile — {exc}")
                try:
                    sb.auth.admin.delete_user(sid)
                except Exception:  # noqa: BLE001
                    r.failures.append(f"student {user}: orphan auth user {sid} could NOT be removed")
                continue
            student_ids[user] = sid
            r.bump(r.created, "student")
            r.student_credentials.append({
                "name": stu["full_name"], "username": user, "class": stu["class_name"],
                "temporary_password": pw,
            })

        cid = class_ids.get(stu["class_name"])
        if cid and student_ids.get(user):
            if _one(sb.table("enrollments").select("id")
                    .eq("class_id", cid).eq("student_id", student_ids[user]).limit(1).execute()):
                r.bump(r.skipped, "enrolment")
            else:
                try:
                    sb.table("enrollments").insert(
                        {"class_id": cid, "student_id": student_ids[user]}).execute()
                    r.bump(r.created, "enrolment")
                except Exception as exc:  # noqa: BLE001
                    r.failures.append(f"enrol {user} in {stu['class_name']}: {exc}")

    # ── parents: invite rows, then links once they exist ─────────────────────
    parent_ids: dict[str, str] = {}
    for p in plan.parents:
        uid = _user_id_for_email(sb, p["email"])
        if uid:
            sb.table("profiles").update({
                "full_name": p["full_name"], "role": "parent",
                **({"ui_locale": p["locale"]} if p.get("locale") else {}),
            }).eq("id", uid).execute()
            parent_ids[p["email"]] = uid
            r.bump(r.updated, "parent")
            continue
        have = _one(sb.table("invites").select("id,token").eq("email", p["email"])
                    .eq("school_id", school_id).is_("accepted_at", "null").limit(1).execute())
        token = have["token"] if have else secrets.token_urlsafe(32)
        if have:
            r.bump(r.skipped, "parent invite (already open)")
        else:
            try:
                sb.table("invites").insert({
                    "email": p["email"], "role": "parent", "school_id": school_id,
                    "token": token, "expires_at": _in_days(invite_days),
                }).execute()
                r.bump(r.created, "parent invite")
            except Exception as exc:  # noqa: BLE001
                r.failures.append(f"parent {p['email']}: {exc}")
                continue
        r.invite_links.append({"name": p["full_name"], "email": p["email"], "role": "parent",
                               "link": f"{site_url}/invite/{token}"})

    for ln in plan.links:
        pid, sid = parent_ids.get(ln["parent_email"]), student_ids.get(ln["username"])
        if not pid or not sid:
            # The parent has not accepted yet. The link is made on acceptance, or
            # by re-running this import afterwards — both are fine, and inventing
            # a parent account to satisfy a foreign key would not be.
            r.bump(r.skipped, "parent link (awaiting acceptance)")
            continue
        if _one(sb.table("parent_links").select("id")
                .eq("parent_id", pid).eq("child_id", sid).limit(1).execute()):
            r.bump(r.skipped, "parent link")
            continue
        try:
            sb.table("parent_links").insert({
                "parent_id": pid, "child_id": sid, "source": "school",
            }).execute()
            r.bump(r.created, "parent link")
        except Exception as exc:  # noqa: BLE001
            r.failures.append(f"link {ln['parent_email']} -> {ln['username']}: {exc}")

    return r


def _user_id_for_email(sb, email: str) -> str | None:
    """The auth user id for an email, or None. Best-effort and never raises."""
    try:
        page = sb.auth.admin.list_users()
        users = page if isinstance(page, list) else getattr(page, "users", []) or []
        for u in users:
            if (getattr(u, "email", "") or "").lower() == email.lower():
                return getattr(u, "id", None)
    except Exception:  # noqa: BLE001
        return None
    return None


def _in_days(n: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat()


def _join_code() -> str:
    alphabet = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
    return "".join(random.choice(alphabet) for _ in range(6))
