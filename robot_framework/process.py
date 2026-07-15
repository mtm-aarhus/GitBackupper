"""This module contains the main process of the robot.

Laver en komplet backup (mirror-klon) af ALLE repos under en organisation
til en backupmappe på en server.
"""

import os
import stat
import shutil
import subprocess
from datetime import datetime

import requests

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement

# Antal ugentlige snapshots pr. repo der beholdes. None = behold alle.
BACKUP_RETENTION = 12

# Zip hvert mirror til én .zip-fil
COMPRESS_BACKUP = True

# Organisationen hvis repos skal backes op
GIT_OWNER = "mtm-aarhus"

GITHUB_API_URL = "https://api.github.com"


def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    """Lav en mirror-backup af alle repos under GIT_OWNER."""
    orchestrator_connection.log_trace("Running git backup process.")

    # Backupmappen hentes fra en OpenOrchestrator-konstant.
    backup_root = orchestrator_connection.get_constant('BackupSti').value

    repositories = _list_repositories(orchestrator_connection)
    orchestrator_connection.log_trace(f"Found {len(repositories)} repositories to back up.")

    failures = []
    for repo in repositories:
        try:
            _backup_repository(repo["name"], repo["clone_url"], backup_root, orchestrator_connection)
        # Vi vil hellere fortsætte med de øvrige repos end at stoppe på ét fejlende.
        # pylint: disable-next=broad-exception-caught
        except Exception as error:
            orchestrator_connection.log_error(f"Backup of '{repo['name']}' failed: {repr(error)}")
            failures.append(repo["name"])

    if failures:
        # Rejs til sidst, så robotten markeres som fejlet i OpenOrchestrator.
        raise RuntimeError(f"{len(failures)} repositories failed to back up: {', '.join(failures)}")


def _list_repositories(orchestrator_connection: OrchestratorConnection) -> list[dict]:
    """Hent alle repos under GIT_OWNER via GitHub-API'et (med paginering).

    Token bruges KUN her — kloningen klarer maskinens git-login selv.
    """
    endpoint = f"{GITHUB_API_URL}/orgs/{GIT_OWNER}/repos"
    params = {"type": "all", "per_page": 100}

    token = orchestrator_connection.get_credential('GitBackupperToken').password
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }

    repositories = []
    page = 1
    while True:
        response = requests.get(endpoint, headers=headers, params={**params, "page": page}, timeout=60)
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        repositories.extend({"name": r["name"], "clone_url": r["clone_url"]} for r in batch)
        page += 1

    return repositories


def _backup_repository(repo_name: str, clone_url: str, backup_root: str, orchestrator_connection: OrchestratorConnection) -> None:
    """Lav en mirror-backup af ét enkelt repo til backupmappen."""
    timestamp = datetime.now().strftime("%Y-%m-%d")

    repo_backup_dir = os.path.join(backup_root, repo_name)
    os.makedirs(repo_backup_dir, exist_ok=True)

    mirror_path = os.path.join(repo_backup_dir, f"{repo_name}_{timestamp}.git")

    # Hvis robotten køres igen samme dag, ryddes den halvfærdige klon først.
    if os.path.exists(mirror_path):
        _remove_path(mirror_path)

    # Ingen credentials i URL'en — git bruger maskinens credential manager.
    orchestrator_connection.log_trace(f"Cloning mirror of {clone_url} -> {mirror_path}")
    _run_git(["git", "clone", "--mirror", clone_url, mirror_path])

    result_path = mirror_path
    if COMPRESS_BACKUP:
        result_path = shutil.make_archive(mirror_path, "zip", root_dir=mirror_path)
        _remove_path(mirror_path)

    orchestrator_connection.log_trace(f"Backup complete: {result_path}")

    _apply_retention(repo_backup_dir, repo_name, orchestrator_connection)


def _run_git(args: list[str]) -> subprocess.CompletedProcess:
    """Kør en git-kommando og rejs en fejl hvis den fejler.

    Git skriver progress til stderr selv når alt går godt, så vi tjekker
    på returncode i stedet for at reagere på stderr-indhold.
    """
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git failed (exit {result.returncode}): {result.stderr.strip()}")
    return result


def _apply_retention(folder: str, repo_name: str, orchestrator_connection: OrchestratorConnection) -> None:
    """Slet de ældste snapshots så der højst er BACKUP_RETENTION tilbage."""
    if not BACKUP_RETENTION:
        return

    # Filnavne er repo_YYYY-MM-DD(.git/.zip) -> alfabetisk sortering = kronologisk.
    entries = sorted(e for e in os.listdir(folder) if e.startswith(f"{repo_name}_"))
    excess = len(entries) - BACKUP_RETENTION
    for old in entries[:max(0, excess)]:
        _remove_path(os.path.join(folder, old))
        orchestrator_connection.log_trace(f"Removed old backup: {old}")


def _remove_path(path: str) -> None:
    """Slet en fil eller mappe. Håndterer read-only git-objekter på Windows."""
    if os.path.isdir(path):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def _force_remove(func, path, _exc) -> None:
    """onexc-handler: git-objekter er read-only på Windows — ryd flaget og prøv igen."""
    os.chmod(path, stat.S_IWRITE)
    func(path)