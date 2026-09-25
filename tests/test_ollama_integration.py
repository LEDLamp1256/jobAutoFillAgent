"""Explicitly gated semantic-only acceptance against an installed local model."""

import os
import shutil
import subprocess
import time
import unittest

from jobagent.domain import AnswerSource, ControlType, QuestionObservation
from jobagent.resolution import (
    CanonicalResult, CanonicalStatus, DeterministicAnswerResolver, ResolutionStatus,
)
from jobagent.semantic_llm import (
    GroundedSemanticResolver, MappingRequest, MappingStatus, OllamaConfig, OllamaSemanticMapper,
)
from tests.test_semantic_llm import SPONSORSHIP, profile


@unittest.skipUnless(os.environ.get("JOB_AGENT_RUN_OLLAMA_TESTS") == "1",
                     "set JOB_AGENT_RUN_OLLAMA_TESTS=1 for installed local Ollama acceptance")
class LocalOllamaAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.executable = shutil.which("ollama")
        if not cls.executable:
            raise RuntimeError("ollama CLI is unavailable; install/configure it before gated acceptance")
        version = subprocess.run([cls.executable, "--version"], capture_output=True, text=True,
                                 check=True, timeout=10)
        listed = subprocess.run([cls.executable, "list"], capture_output=True, text=True,
                                check=True, timeout=10)
        cls.model = os.environ.get("JOB_AGENT_OLLAMA_MODEL", "llama3.1")
        names = {line.split()[0] for line in listed.stdout.splitlines()[1:] if line.split()}
        if cls.model not in names:
            raise RuntimeError(f"configured Ollama model {cls.model!r} is not installed")
        print(f"Ollama acceptance: {version.stdout.strip() or version.stderr.strip()}, model={cls.model}")

    async def test_semantic_mapping_without_candidate_values(self):
        mapper = OllamaSemanticMapper(OllamaConfig(model=self.model, timeout_seconds=60))
        cases = (
            ("sponsorship", MappingRequest(SPONSORSHIP, "Employment", None, ControlType.CHOICE,
                                            ("Yes", "No"),
                                            ("employment.sponsorship", "employment.us_authorized")),
             MappingStatus.MATCH, "employment.sponsorship"),
            ("work_authorization", MappingRequest("Are you legally permitted to work in the US?",
                                                   "Employment", None, ControlType.CHOICE, ("Yes", "No"),
                                                   ("employment.sponsorship", "employment.us_authorized")),
             MappingStatus.MATCH, "employment.us_authorized"),
            ("narrative", MappingRequest("Why do you want to work here?",
                                         "Application questions", None, ControlType.TEXT, (),
                                         ("personal.email", "employment.sponsorship")),
             MappingStatus.NO_MATCH, None),
            ("out_of_domain", MappingRequest("Can you juggle three oranges?", None, None,
                                             ControlType.TEXT, (),
                                             ("personal.email", "employment.sponsorship")),
             MappingStatus.NO_MATCH, None),
        )
        for name, request, expected_status, expected_key in cases:
            with self.subTest(name=name):
                start = time.monotonic()
                result = await mapper.map_question(request)
                elapsed = time.monotonic() - start
                question = QuestionObservation(request.label, request.control_type,
                                               section=request.section, options=request.options)
                deterministic = DeterministicAnswerResolver(profile())
                if result.status is MappingStatus.MATCH and result.semantic_key is not None:
                    canonical = CanonicalResult(CanonicalStatus.MATCHED, result.semantic_key, "local_llm")
                    resolved = deterministic.resolve_known_key(question, "acceptance", canonical)
                else:
                    resolved = await GroundedSemanticResolver(deterministic, mapper).resolve(
                        question, "acceptance")
                print(f"{name}: raw_status={result.raw_status}, validated={result.status.value}, "
                      f"key={result.semantic_key}, resolver={resolved.status.value}, "
                      f"answer_source={resolved.answer.source.value if resolved.answer else None}, "
                      f"seconds={elapsed:.2f}")
                self.assertEqual(result.status, expected_status, result.reason)
                self.assertEqual(result.semantic_key, expected_key)
                self.assertEqual(resolved.status, ResolutionStatus.SAFE_TO_FILL if expected_key else
                                 (ResolutionStatus.REQUIRES_REVIEW if name == "narrative" else
                                  ResolutionStatus.UNRESOLVED))
                if expected_key:
                    self.assertEqual(resolved.answer.source, AnswerSource.CANDIDATE_PROFILE)
                else:
                    self.assertIsNone(resolved.answer)

    async def test_real_mapping_is_grounded_in_profile_values(self):
        mapper = OllamaSemanticMapper(OllamaConfig(model=self.model, timeout_seconds=60))

        class CapturingMapper:
            async def map_question(self, request):
                self.last = await mapper.map_question(request)
                return self.last

        captured = CapturingMapper()
        resolver = GroundedSemanticResolver(DeterministicAnswerResolver(profile()), captured)
        for label, key in ((SPONSORSHIP, "employment.sponsorship"),
                           ("Are you legally permitted to work in the US?", "employment.us_authorized")):
            with self.subTest(key=key):
                question = QuestionObservation(label, ControlType.CHOICE, section="Employment",
                                               options=("Yes", "No"))
                result = await resolver.resolve(question, "acceptance")
                print(f"grounded {key}: raw_status={captured.last.raw_status}, "
                      f"validated={captured.last.status.value}, proposed_key={captured.last.semantic_key}, "
                      f"resolver={result.status.value}, reason={captured.last.reason}")
                self.assertEqual(result.status, ResolutionStatus.SAFE_TO_FILL)
                self.assertEqual(result.canonical.semantic_key, key)
                self.assertEqual(result.mapping_source, AnswerSource.LOCAL_LLM)
                self.assertEqual(result.answer.source, AnswerSource.CANDIDATE_PROFILE)
        self.assertEqual(resolver.call_count, 2)


if __name__ == "__main__":
    unittest.main()
