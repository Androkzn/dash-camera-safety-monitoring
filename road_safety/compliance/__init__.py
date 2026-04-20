"""Compliance modules: audit logging, data retention.

Python note (for newcomers)
---------------------------
Any directory that contains an ``__init__.py`` file is treated by Python as a
"package" — a folder you can import from using dotted paths like
``from road_safety.compliance import audit``. The file can be empty; its mere
presence is what makes the folder a package. A module-level docstring (the
triple-quoted string you are reading) is the conventional place to describe
what the package does.

What lives here
---------------
* ``audit.py``     — append-only audit log at ``data/audit.jsonl``. Every
                     sensitive access (unredacted thumbnails, admin endpoints,
                     DSAR attempts, retention sweeps) writes one JSON line.
                     Controlled by the ``ROAD_AUDIT_LOG`` env var.
* ``retention.py`` — hourly background sweep that deletes old thumbnails,
                     trims JSONL logs, and removes stale active-learning
                     samples per the ``ROAD_RETENTION_*_DAYS`` env vars.
                     Every deletion is written to the audit log so sweeps
                     leave an evidentiary trail.

Why a separate package
----------------------
Regulators (GDPR Art. 5/30, SOC 2) ask two distinct questions: "who accessed
what?" and "how long did you keep it?". Splitting those concerns into their
own package makes the answer to each question trivially discoverable in the
source tree — auditors can read one directory and see the whole compliance
posture.
"""
