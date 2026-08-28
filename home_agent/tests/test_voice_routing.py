from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).parents[2]
HOME_AGENT = REPOSITORY / "home_agent"
NAMED_ORCHESTRATORS = {"jarvis", "jasmine"}
CONTROLLER_SPEC = importlib.util.spec_from_file_location(
    "voice_routing_controller", HOME_AGENT / "home_agent.py"
)
assert CONTROLLER_SPEC and CONTROLLER_SPEC.loader
CONTROLLER = importlib.util.module_from_spec(CONTROLLER_SPEC)
sys.modules[CONTROLLER_SPEC.name] = CONTROLLER
CONTROLLER_SPEC.loader.exec_module(CONTROLLER)


class VoiceRoutingContractTests(unittest.TestCase):
    def test_all_voice_agents_are_fail_closed_compatibility_ingresses(self) -> None:
        voice_files = sorted(
            [*(REPOSITORY / "agent").glob("voice-*.md")]
            + [*(HOME_AGENT / "agent").glob("voice-*.md")]
        )
        self.assertEqual(len(voice_files), 11)

        home_source = (HOME_AGENT / "agent" / "home_agent.md").read_text(
            encoding="utf-8"
        )
        home_model = next(
            line for line in home_source.splitlines() if line.startswith("model: ")
        )
        for path in voice_files:
            source = path.read_text(encoding="utf-8")
            self.assertIn(home_model, source, path.name)
            self.assertIn('permission:\n  "*": deny\n', source, path.name)
            self.assertIn("fail-closed compatibility ingress", source, path.name)
            self.assertNotIn("permission:\n  bash: allow", source, path.name)

    def test_home_agent_route_targets_a_guarded_voice_ingress(self) -> None:
        route = json.loads(
            (HOME_AGENT / "voice" / "route.json").read_text(encoding="utf-8")
        )
        self.assertTrue(route["always_confirm"])
        self.assertEqual(route["safe_agent"], "voice-home-agent")
        self.assertEqual(route["risky_agent"], "voice-home-agent")

    def test_voice_ingresses_are_reserved_from_project_worker_selection(self) -> None:
        voice_names = {
            path.stem
            for directory in (REPOSITORY / "agent", HOME_AGENT / "agent")
            for path in directory.glob("voice-*.md")
        }
        self.assertEqual(CONTROLLER.VOICE_INGRESS_AGENTS, voice_names)

        settings = CONTROLLER.Settings(
            root=HOME_AGENT,
            state_file=HOME_AGENT / "state" / "runtime.json",
            tasks_file=HOME_AGENT / "state" / "tasks.json",
            lock_file=HOME_AGENT / "state" / ".lock",
            catalog_file=HOME_AGENT / "tests" / "fixtures" / "voice-catalog.md",
            routes_file=HOME_AGENT / "voice" / "route.json",
            server_env=HOME_AGENT / "README.md",
            api_url="http://127.0.0.1:4096",
        )
        project = CONTROLLER.Project(
            name="Home Agent", path=HOME_AGENT, note=""
        )
        for agent in sorted(voice_names):
            with self.assertRaisesRegex(CONTROLLER.HomeAgentError, "reserved"):
                CONTROLLER.select_agent(settings, object(), project, agent)

        self.assertEqual(
            CONTROLLER.HOME_ORCHESTRATOR_AGENTS,
            {"home_agent", *NAMED_ORCHESTRATORS},
        )
        for agent in sorted(NAMED_ORCHESTRATORS):
            with self.assertRaisesRegex(CONTROLLER.HomeAgentError, "own worker"):
                CONTROLLER.select_agent(settings, object(), project, agent)

    def test_named_orchestrators_are_full_guarded_primaries(self) -> None:
        expected_models = {
            "jarvis": "model: openai/gpt-5.6-sol",
            "jasmine": "model: openrouter/thinkingmachines/inkling:free",
        }
        for name, model in expected_models.items():
            source = (HOME_AGENT / "agent" / f"{name}.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("mode: primary", source)
            self.assertIn(model, source)
            self.assertIn('permission:\n  "*": deny\n', source)
            self.assertIn("question: allow", source)
            self.assertIn("doom_loop: deny", source)
            self.assertIn('external_directory:\n    "*": deny\n', source)
            self.assertIn('"sudo *": deny', source)
            self.assertIn("signed_in_tabs_browser_navigate: allow", source)
            self.assertIn("signed_in_tabs_browser_run_code_unsafe: deny", source)
            self.assertIn("signed_in_tabs_browser_file_upload: deny", source)

        jasmine = (HOME_AGENT / "agent" / "jasmine.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("signed_in_tabs_browser_tabs: ask", jasmine)
        self.assertIn("Accept text transcript parts only", jasmine)

    def test_tracked_config_pins_private_browser_without_absolute_plugins(self) -> None:
        config = json.loads(
            (REPOSITORY / "config" / "opencode.jsonc").read_text(encoding="utf-8")
        )
        model = config["provider"]["openrouter"]["models"][
            "thinkingmachines/inkling:free"
        ]
        self.assertEqual(model["cost"], {"input": 0, "output": 0})
        self.assertEqual(model["modalities"]["output"], ["text"])

        command = config["mcp"]["signed_in_tabs"]["command"]
        self.assertEqual(command[0:3], ["npx", "-y", "@playwright/mcp@0.0.79"])
        self.assertIn("--extension", command)
        self.assertIn("--image-responses", command)
        self.assertNotIn("--allow-unrestricted-file-access", command)
        self.assertNotIn("--allowed-origins", command)
        self.assertNotIn("--caps", command)

        self.assertNotIn("plugin", config)

    def test_signed_in_browser_is_not_granted_to_other_tracked_agents(self) -> None:
        for directory in (REPOSITORY / "agent", HOME_AGENT / "agent"):
            for path in directory.glob("*.md"):
                if path.stem in NAMED_ORCHESTRATORS:
                    continue
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("signed_in_tabs_", source, path.name)

    def test_primary_home_agent_protections_remain_explicit(self) -> None:
        source = (HOME_AGENT / "agent" / "home_agent.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("mode: primary", source)
        self.assertIn('permission:\n  "*": deny\n', source)
        self.assertIn("question: allow", source)
        self.assertIn("doom_loop: deny", source)
        self.assertIn('external_directory:\n    "*": deny\n', source)
        self.assertIn("Never delete, move, or rename any file or directory", source)

        controller = (HOME_AGENT / "home_agent.py").read_text(encoding="utf-8")
        self.assertIn("if selected in HOME_ORCHESTRATOR_AGENTS:", controller)
        self.assertIn('f"{selected} cannot be selected as its own worker"', controller)

    def test_desktop_push_to_talk_always_submits_agent_mode(self) -> None:
        source = (
            REPOSITORY
            / "keymapping"
            / "gnome-extension"
            / "voice-launch@local"
            / "extension.js"
        ).read_text(encoding="utf-8")
        self.assertIn("replace_contents(\n                'agent'", source)
        self.assertNotIn("'dictate'", source)
        self.assertNotIn("focus_window", source)
        self.assertNotIn("TERMINAL_HINTS", source)
        self.assertIn("/.local/bin/voice-ptt", source)


if __name__ == "__main__":
    unittest.main()
