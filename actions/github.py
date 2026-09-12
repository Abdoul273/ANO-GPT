"""Contrôle GitHub : OAuth, dépôts, commit/push sûr et état local."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from actions.devsecops import GitMaster
from core.github_service import GitHubError, GitHubSetupRequired, get_github_service

from core import action_kit as kit

_PROJECT_ROOTS = (Path.home() / "OUTILS", Path.home() / "Documents", Path.home() / "Projets")


def _run(args: list[str], cwd: Path, *, timeout: float = 45, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    proc = kit.run(args, cwd=str(cwd), timeout=timeout, env=env)
    if proc.timed_out:
        return -1, "", "Délai Git dépassé."
    if proc.not_found:
        return -1, "", proc.reason()
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _project(value: str) -> Path:
    raw = str(value or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    name = Path(raw).name.casefold()
    matches: list[Path] = []
    for root in _PROJECT_ROOTS:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and child.name.casefold() == name:
                matches.append(child.resolve())
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise GitHubError(f"Plusieurs projets portent le nom « {raw} » : indiquez son chemin complet.")
    raise GitHubError(f"Projet introuvable : « {raw} ». Indiquez son dossier ou placez-le dans ~/OUTILS, ~/Documents ou ~/Projets.")


def _ensure_git(path: Path) -> None:
    if not shutil.which("git"):
        raise GitHubError("Git est absent. Installez-le avec sudo pacman -S git.")
    code, out, _ = _run(["git", "rev-parse", "--is-inside-work-tree"], path)
    if code == 0 and out == "true":
        return
    code, _, err = _run(["git", "init"], path)
    if code != 0:
        raise GitHubError(f"Initialisation Git impossible : {err}")


def _branch(path: Path) -> str:
    code, out, _ = _run(["git", "branch", "--show-current"], path)
    return out if code == 0 and out else "main"


def _remote(path: Path) -> str:
    code, out, _ = _run(["git", "remote", "get-url", "origin"], path)
    return out if code == 0 else ""


def _set_remote(path: Path, url: str) -> None:
    if _remote(path):
        code, _, err = _run(["git", "remote", "set-url", "origin", url], path)
    else:
        code, _, err = _run(["git", "remote", "add", "origin", url], path)
    if code != 0:
        raise GitHubError(f"Remote GitHub impossible à configurer : {err}")


def _askpass_env(token: str) -> tuple[dict[str, str], Path]:
    fd, filename = tempfile.mkstemp(prefix="anogpt-git-askpass-", text=True)
    path = Path(filename)
    try:
        os.write(fd, b'#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" x-access-token ;; *) printf "%s\\n" "$ANOGPT_GITHUB_TOKEN" ;; esac\n')
    finally:
        os.close(fd)
    os.chmod(path, 0o700)
    return {"GIT_ASKPASS": str(path), "GIT_TERMINAL_PROMPT": "0", "ANOGPT_GITHUB_TOKEN": token}, path


def _scan_outgoing(path: Path) -> str | None:
    code, upstream, _ = _run(["git", "rev-parse", "--abbrev-ref", "@{u}"], path)
    if code == 0:
        code, diff, err = _run(["git", "diff", f"{upstream}...HEAD"], path)
    else:
        code, empty_tree, err = _run(["git", "hash-object", "-t", "tree", "/dev/null"], path)
        diff = "" if code != 0 else _run(["git", "diff", empty_tree, "HEAD"], path)[1]
    if code != 0:
        return None
    findings = GitMaster.scan_diff_for_secrets(diff)
    if findings:
        kinds = ", ".join(sorted({kind for kind, _ in findings}))
        return f"🚨 PUSH BLOQUÉ PAR SÉCURITÉ : secrets potentiels détectés ({kinds}). Corrigez-les avant toute publication."
    return None


def _push(path: Path, branch: str) -> str:
    remote = _remote(path)
    if not remote:
        raise GitHubError("Aucun remote GitHub. Créez ou associez d'abord un dépôt.")
    blocked = _scan_outgoing(path)
    if blocked:
        return blocked
    env, askpass = _askpass_env(get_github_service().token())
    try:
        code, out, err = _run(["git", "push", "-u", "origin", branch], path, timeout=90, env=env)
    finally:
        askpass.unlink(missing_ok=True)
    if code == 0:
        return f"✅ Push GitHub réussi sur origin/{branch}."
    detail = f"{out}\n{err}".lower()
    if any(word in detail for word in ("non-fast-forward", "fetch first", "rejected")):
        return (
            "⚠️ Push refusé : le dépôt distant contient des commits absents localement. "
            "Options : demandez « récupère GitHub puis rebase [projet] » pour intégrer proprement les changements, "
            "ou résolvez les conflits avant de réessayer. Aucun pull forcé n'a été fait."
        )
    return f"Push GitHub échoué : {err or out}"


def _create_repo(name: str, private: bool) -> dict[str, Any]:
    safe_name = str(name or "").strip()
    if not safe_name:
        raise GitHubError("Donnez un nom de dépôt GitHub.")
    return get_github_service().api("POST", "/user/repos", payload={
        "name": safe_name, "private": bool(private), "auto_init": False,
        "description": f"Dépôt créé par ANO-GPT pour {safe_name}",
    })


def _repo_slug(path: Path) -> str:
    """« owner/repo » depuis l'URL du remote origin (https ou ssh)."""
    remote = _remote(path)
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", remote)
    if not m:
        raise GitHubError("Le remote origin n'est pas un dépôt GitHub.")
    return f"{m.group(1)}/{m.group(2)}"


def _pull(path: Path, branch: str) -> str:
    """Récupère et rebase proprement : jamais de pull forcé, les modifications
    locales sont mises de côté puis restaurées."""
    if not _remote(path):
        raise GitHubError("Aucun remote GitHub configuré pour ce projet.")
    env, askpass = _askpass_env(get_github_service().token())
    try:
        code, _, err = _run(["git", "fetch", "origin", branch], path, timeout=90, env=env)
    finally:
        askpass.unlink(missing_ok=True)
    if code != 0:
        return f"Récupération GitHub échouée : {err}"
    _, dirty, _ = _run(["git", "status", "--porcelain"], path)
    stashed = False
    if dirty.strip():
        code, _, err = _run(["git", "stash", "push", "--include-untracked", "-m", "anogpt-pull"], path)
        stashed = code == 0
    code, out, err = _run(["git", "rebase", f"origin/{branch}"], path, timeout=60)
    if code != 0:
        _run(["git", "rebase", "--abort"], path)
        if stashed:
            _run(["git", "stash", "pop"], path)
        return ("⚠️ Rebase impossible sans conflit : rien n'a été modifié. "
                "Résolvez les conflits à la main ou demandez-moi le détail des différences.")
    if stashed:
        code, _, err = _run(["git", "stash", "pop"], path)
        if code != 0:
            return ("Dépôt à jour, mais vos modifications locales mises de côté n'ont pas pu être "
                    "réappliquées automatiquement : elles sont dans `git stash list`.")
    _, last, _ = _run(["git", "log", "-1", "--format=%h %s"], path)
    return f"✅ {path.name} à jour sur origin/{branch}. Dernier commit : {last}."


def _log(path: Path, count: int) -> str:
    _ensure_git(path)
    _, out, err = _run(["git", "log", f"-{count}", "--format=%h · %ar · %an · %s"], path)
    if not out:
        return f"Aucun commit dans {path.name}." if not err else f"Historique illisible : {err}"
    return f"Derniers commits de {path.name} :\n" + out


def _changes(path: Path) -> str:
    _ensure_git(path)
    _, short, _ = _run(["git", "status", "--short"], path)
    _, stat, _ = _run(["git", "diff", "--stat", "HEAD"], path)
    if not short.strip():
        return f"Aucune modification en attente dans {path.name}."
    files = [line for line in short.splitlines() if line.strip()]
    summary = stat.splitlines()[-1].strip() if stat.strip() else ""
    return (f"{len(files)} fichier(s) modifié(s) dans {path.name}" + (f" ({summary})" if summary else "")
            + " :\n" + "\n".join(files[:40]) + ("\n…" if len(files) > 40 else ""))


def _issues(path: Path, state: str, kind: str) -> str:
    slug = _repo_slug(path)
    endpoint = "pulls" if kind == "pulls" else "issues"
    items = get_github_service().api("GET", f"/repos/{slug}/{endpoint}?state={state}&per_page=30")
    if not isinstance(items, list):
        raise GitHubError("Réponse GitHub invalide.")
    if kind == "issues":
        items = [i for i in items if isinstance(i, dict) and "pull_request" not in i]
    label = "pull requests" if kind == "pulls" else "issues"
    if not items:
        return f"Aucune {label[:-1] if kind == 'pulls' else 'issue'} {state} sur {slug}."
    return f"{len(items)} {label} ({state}) sur {slug} :\n" + "\n".join(
        f"- #{i.get('number')} {i.get('title')} — {i.get('user', {}).get('login', '?')}"
        for i in items if isinstance(i, dict))


def _create_issue(path: Path, title: str, body: str) -> str:
    if not title:
        raise GitHubError("Donnez un titre à l'issue.")
    slug = _repo_slug(path)
    issue = get_github_service().api("POST", f"/repos/{slug}/issues",
                                     payload={"title": title, "body": body or ""})
    return f"Issue créée : #{issue.get('number')} {issue.get('title')} — {issue.get('html_url')}"


def _clone(url: str, dest: str) -> str:
    url = str(url or "").strip()
    if not url:
        raise GitHubError("Donnez l'URL ou le « owner/repo » à cloner.")
    if re.fullmatch(r"[\w.-]+/[\w.-]+", url):
        url = f"https://github.com/{url}.git"
    root = Path(dest).expanduser() if dest else _PROJECT_ROOTS[0]
    root.mkdir(parents=True, exist_ok=True)
    name = Path(url.rstrip("/")).name.removesuffix(".git")
    target = root / name
    if target.exists():
        return f"Le dossier {target} existe déjà."
    env, askpass = _askpass_env(get_github_service().token())
    try:
        code, _, err = _run(["git", "clone", url, str(target)], root, timeout=300, env=env)
    finally:
        askpass.unlink(missing_ok=True)
    return f"✅ Cloné dans {target}." if code == 0 else f"Clonage échoué : {err}"


def _status(path: Path) -> str:
    _ensure_git(path)
    branch = _branch(path)
    _, changes, _ = _run(["git", "status", "--short"], path)
    _, last, _ = _run(["git", "log", "-1", "--format=%h %s"], path)
    return f"{path.name} — branche {branch}\nDernier commit : {last or 'aucun'}\nModifications en attente : {len([x for x in changes.splitlines() if x])}."


@kit.action("github_control")
def github_control(parameters: dict | None = None, ui=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().casefold()
    try:
        if action in {"connect", "login", "authorize"}:
            log = getattr(ui, "write_log", None)
            def show_device_code(code: str, _url: str) -> None:
                if ui is None or not hasattr(ui, "show_card"):
                    return
                ui.show_card(
                    "github-oauth",
                    "Autorisation GitHub",
                    "Dans Google Chrome, saisissez ce code :\n\n"
                    f"# {code}\n\n"
                    "Ne partagez ce code avec personne. Cette carte restera visible "
                    "jusqu'à la fin ou l'annulation de la connexion.",
                )

            try:
                profile = get_github_service().connect(
                    on_progress=log if callable(log) else None,
                    on_device_code=show_device_code,
                )
            finally:
                # Expiration et refus ferment également la carte : elle ne doit
                # jamais laisser croire qu'un ancien code est encore valable.
                if ui is not None and hasattr(ui, "dismiss_cards"):
                    ui.dismiss_cards("github-oauth", "Autorisation GitHub")
            return f"GitHub est connecté pour {profile.get('login') or 'votre compte'} ; le jeton est dans le trousseau système."
        if action in {"disconnect", "revoke", "logout"}:
            return get_github_service().disconnect()
        if action in {"status", "diagnostic"}:
            status = get_github_service().status(verify=True)
            prefix = f"GitHub est connecté pour {status.login}." if status.authenticated else status.message
            return prefix + (f" Prochaine étape : {status.next_step}" if status.next_step else "")
        if action in {"list", "repos", "list_repos"}:
            repos = get_github_service().api("GET", "/user/repos?per_page=100&sort=updated")
            if not isinstance(repos, list):
                raise GitHubError("Réponse GitHub invalide lors de la liste des dépôts.")
            if not repos:
                return "Aucun dépôt GitHub trouvé."
            return "Vos dépôts GitHub :\n" + "\n".join(
                f"- {item.get('full_name', item.get('name', '?'))} · {item.get('default_branch', 'main')} · "
                f"mis à jour {str(item.get('updated_at') or '')[:10]}"
                for item in repos[:100] if isinstance(item, dict)
            )

        project_ref = str(params.get("project") or params.get("path") or "")
        if action in {"clone"}:
            return _clone(str(params.get("url") or params.get("repo_name") or ""), str(params.get("dest") or ""))
        if action in {"create_repo", "create", "new_repo"} and not project_ref:
            repo = _create_repo(str(params.get("repo_name") or ""), bool(params.get("private", True)))
            return f"Dépôt GitHub créé : {repo.get('full_name')}."
        path = _project(project_ref)
        if action in {"backup_enable", "backup_disable"}:
            from memory.config_manager import _default_manager
            config = _default_manager._file.read()
            tracked = config.get("github_backup_projects", [])
            tracked = [str(item) for item in tracked] if isinstance(tracked, list) else []
            canonical = str(path)
            if action == "backup_enable" and canonical not in tracked:
                tracked.append(canonical)
            if action == "backup_disable":
                tracked = [item for item in tracked if item != canonical]
            _default_manager._file.update({"github_backup_projects": tracked})
            _default_manager._cache = None
            return (f"Sauvegarde GitHub périodique activée pour {path.name}."
                    if action == "backup_enable" else f"Sauvegarde GitHub périodique arrêtée pour {path.name}.")
        if action in {"init", "initialize"}:
            _ensure_git(path)
            return f"Git initialisé pour {path.name}."
        if action in {"create_repo", "create", "new_repo"}:
            _ensure_git(path)
            repo = _create_repo(str(params.get("repo_name") or path.name), bool(params.get("private", True)))
            _set_remote(path, str(repo.get("clone_url") or ""))
            return f"Dépôt GitHub créé et remote origin ajouté : {repo.get('full_name')}."
        if action in {"commit", "commit_push", "push"}:
            _ensure_git(path)
            summary = ""
            if action in {"commit", "commit_push"}:
                summary = GitMaster.smart_commit(str(params.get("message") or "") or None, repo_path=path)
                if "BLOQUÉ" in summary or "Échec" in summary:
                    return summary
            if action in {"push", "commit_push"}:
                if not _remote(path) and action == "commit_push":
                    repo = _create_repo(str(params.get("repo_name") or path.name), bool(params.get("private", True)))
                    _set_remote(path, str(repo.get("clone_url") or ""))
                branch = str(params.get("branch") or _branch(path))
                return (summary + "\n" if summary else "") + _push(path, branch)
            return summary
        if action in {"project_status", "local_status"}:
            return _status(path)
        if action in {"pull", "sync", "fetch", "rebase", "update"}:
            _ensure_git(path)
            return _pull(path, str(params.get("branch") or _branch(path)))
        if action in {"log", "history", "commits"}:
            try:
                count = max(1, min(int(params.get("count") or 10), 50))
            except (TypeError, ValueError):
                count = 10
            return _log(path, count)
        if action in {"changes", "diff", "pending"}:
            return _changes(path)
        if action in {"issues", "list_issues"}:
            return _issues(path, str(params.get("state") or "open"), "issues")
        if action in {"prs", "pull_requests", "list_prs"}:
            return _issues(path, str(params.get("state") or "open"), "pulls")
        if action in {"create_issue", "new_issue", "issue"}:
            return _create_issue(path, str(params.get("title") or params.get("message") or ""),
                                 str(params.get("body") or ""))
        raise GitHubError("Action GitHub inconnue : status, connect, disconnect, list, clone, init, create_repo, "
                          "commit, push, commit_push, pull, log, changes, issues, prs, create_issue ou backup_enable.")
    except (GitHubError, GitHubSetupRequired) as exc:
        return str(exc)
