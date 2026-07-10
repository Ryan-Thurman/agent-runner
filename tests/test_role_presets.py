import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from agent_runner.config import (
    SAMPLE_CONFIG,
    apply_preset,
    fallback_profile_names,
    load_config,
    strip_json_comments,
)
from agent_runner.errors import ConfigError


ROOT = Path(__file__).resolve().parents[1]


def run_cli(cwd: Path, home: Path, *args: str, extra_env: Optional[dict[str, str]] = None):
    env = os.environ.copy()
    env["AGENT_RUNNER_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "agent_runner", *args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def write_config(repo: Path, **overrides) -> None:
    data = json.loads(strip_json_comments(SAMPLE_CONFIG))
    data["planPath"] = "docs/plan.md"
    data["checks"] = []
    data["autoFixAttempts"] = 0
    data.pop("presets", None)
    data.update(overrides)
    (repo / ".agent-runner.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


class RoleResolutionTests(unittest.TestCase):
    def _load(self, **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_config(repo, **overrides)
            return load_config(repo)

    def test_closer_derives_from_the_coder_when_unnamed(self):
        config = self._load(roles={"coder": "codex", "reviewer": "claude-opus"})
        self.assertEqual(config.roles["closer"], "codex")
        self.assertNotIn("closer", config.declared_roles)

    def test_closer_can_be_pinned_independently_of_the_coder(self):
        config = self._load(
            roles={"coder": "codex", "reviewer": "claude-opus", "closer": "claude-opus"}
        )
        self.assertEqual(config.roles["closer"], "claude-opus")
        self.assertEqual(config.declared_roles["closer"], "claude-opus")

    def test_triage_derives_from_the_cheap_review_tier(self):
        config = self._load(
            reviewTriage={"simple": "claude-sonnet", "complex": "claude-opus"}
        )
        self.assertEqual(config.roles["triage"], "claude-sonnet")

    def test_unknown_role_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "invalid roles.summarizer"):
            self._load(
                roles={"coder": "codex", "reviewer": "claude-opus", "summarizer": "codex"}
            )

    def test_closer_and_fixer_inherit_the_coders_fallback_chain(self):
        config = self._load(
            roles={"coder": "codex", "reviewer": "claude-opus", "fixer": "claude-opus"},
            roleFallbacks={"coder": ["claude-sonnet"]},
        )
        self.assertEqual(fallback_profile_names(config, "closer"), ["claude-sonnet"])
        self.assertEqual(fallback_profile_names(config, "fixer"), ["claude-sonnet"])

    def test_triage_inherits_the_reviewers_fallback_chain(self):
        config = self._load(roleFallbacks={"reviewer": ["antigravity"]})
        self.assertEqual(fallback_profile_names(config, "triage"), ["antigravity"])

    def test_an_explicit_chain_is_never_overridden_by_inheritance(self):
        config = self._load(
            roles={"coder": "codex", "reviewer": "claude-opus", "closer": "codex"},
            roleFallbacks={"coder": ["claude-sonnet"], "closer": []},
        )
        self.assertEqual(fallback_profile_names(config, "closer"), [])


class PresetTests(unittest.TestCase):
    def _load(self, **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_config(repo, **overrides)
            return load_config(repo)

    def test_preset_moves_the_derived_closer_along_with_the_coder(self):
        config = self._load(
            roles={"coder": "codex", "reviewer": "claude-opus"},
            presets={"claude": {"coder": "claude-opus"}},
        )
        self.assertEqual(config.roles["closer"], "codex")

        switched = apply_preset(config, "claude")
        self.assertEqual(switched.roles["coder"], "claude-opus")
        self.assertEqual(switched.roles["closer"], "claude-opus")
        self.assertEqual(switched.active_preset, "claude")
        # The original config is untouched.
        self.assertEqual(config.roles["coder"], "codex")

    def test_preset_leaves_a_pinned_closer_pinned(self):
        config = self._load(
            roles={"coder": "codex", "reviewer": "claude-opus", "closer": "claude-opus"},
            presets={"all-codex": {"coder": "codex", "reviewer": "codex"}},
        )
        switched = apply_preset(config, "all-codex")
        self.assertEqual(switched.roles["coder"], "codex")
        self.assertEqual(switched.roles["closer"], "claude-opus")

    def test_preset_referencing_an_unknown_profile_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown agent profile"):
            self._load(presets={"bad": {"coder": "gpt-9"}})

    def test_preset_referencing_an_unknown_role_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "invalid presets.bad.summarizer"):
            self._load(presets={"bad": {"summarizer": "codex"}})

    def test_applying_an_unknown_preset_is_rejected(self):
        config = self._load(presets={"claude": {"coder": "claude-opus"}})
        with self.assertRaisesRegex(ConfigError, "unknown preset 'nope'"):
            apply_preset(config, "nope")


class AgentsCommandTests(unittest.TestCase):
    def test_agents_shows_swaps_and_clears_the_active_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init(repo)
            write_config(
                repo,
                roles={"coder": "codex", "reviewer": "claude-opus"},
                roleFallbacks={"coder": ["claude-sonnet"]},
                presets={"claude": {"coder": "claude-opus"}},
            )

            listed = run_cli(repo, home, "agents")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertIn("preset: none (config roles)", listed.stdout)
            self.assertIn("coder     -> codex fallback: claude-sonnet", listed.stdout)
            # The closer is derived, and inherits the coder's chain.
            self.assertIn(
                "closer    -> codex [derived] fallback: claude-sonnet", listed.stdout
            )
            self.assertIn("presets: claude", listed.stdout)

            switched = run_cli(repo, home, "agents", "--use", "claude")
            self.assertEqual(switched.returncode, 0, switched.stderr)
            self.assertIn("switched to preset 'claude'", switched.stderr)
            self.assertIn("preset: claude", switched.stdout)
            self.assertIn("coder     -> claude-opus", switched.stdout)
            self.assertIn("closer    -> claude-opus", switched.stdout)

            # The swap survives a fresh process: it lives in the runner db.
            again = run_cli(repo, home, "agents")
            self.assertIn("preset: claude", again.stdout)
            status = run_cli(repo, home, "status")
            self.assertIn("[agent-runner] agent preset: claude", status.stderr)

            cleared = run_cli(repo, home, "agents", "--clear")
            self.assertEqual(cleared.returncode, 0, cleared.stderr)
            self.assertIn("cleared the agent preset", cleared.stderr)
            self.assertIn("preset: none (config roles)", cleared.stdout)
            self.assertIn("coder     -> codex", cleared.stdout)

    def test_agents_use_rejects_an_unknown_preset_without_touching_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo, presets={"claude": {"coder": "claude-opus"}})

            result = run_cli(repo, home, "agents", "--use", "nope")
            self.assertEqual(result.returncode, 1)
            self.assertIn("unknown preset 'nope'", result.stderr)

            listed = run_cli(repo, home, "agents")
            self.assertIn("preset: none (config roles)", listed.stdout)

    def test_agents_warns_when_the_active_preset_leaves_the_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo, presets={"claude": {"coder": "claude-opus"}})
            run_cli(repo, home, "agents", "--use", "claude")

            write_config(repo)  # the presets block is gone
            listed = run_cli(repo, home, "agents")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertIn("is no longer defined", listed.stderr)
            self.assertIn("preset: none (config roles)", listed.stdout)


if __name__ == "__main__":
    unittest.main()
