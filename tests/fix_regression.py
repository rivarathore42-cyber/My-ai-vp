"""Section R of the suite: regression checks for the audit items fixed after RC-01 to RC-05.

Each check is one audit probe (A1 to A20) that failed on the audited code and now passes. The probes are the spec:
a request for 'times' is math, a note must share most of a question's words to answer it, a poem request is not a
haiku check, a claimed wrong result is reported, and so on.
"""
import asyncio
import base64
import struct
import tempfile
import zlib
from pathlib import Path

from galaxy import agents_system as S
from galaxy import agents_text as AT
from galaxy import clauses, code_analysis, gateway, mathlogic, prompts, reflection, search_tools, semantic
from galaxy import simulate, voice, wiki_parse
from galaxy.agents_web import GeneralKnowledgeAgent, MemoryVaultAgent, SearchAgent
from galaxy.core import ApiGateway
from galaxy.memory import MemoryStore, _percentile


def _store() -> MemoryStore:
    return MemoryStore(Path(tempfile.mkdtemp(prefix="fixr_")) / "f.db")


def _png(width: int, height: int) -> bytes:
    chunk = lambda tag, body: struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IEND", b""))


async def run(check) -> None:
    # A1 and A4: spoken operators are math; a wrong claimed result is reported, not silently replaced
    check(clauses.classify_intent("what is 5 times 4") == "math", "A1-01: 'times' is routed to math")
    check(clauses.classify_intent("what is 10 divided by 2") == "math", "A1-02: 'divided by' is routed to math")
    agent = mathlogic.MathLogicAgent()
    check("20" in (await agent.process("what is 5 times 4", {})).text, "A1: '5 times 4' is answered as 20")
    check("30" in (await agent.process("15 percent of 200", {})).text, "A4-01: '15 percent of 200' is answered as 30")
    check("mismatch" in (await agent.process("check: 2 + 3 = 6", {})).text.lower(),
          "A4-02: a wrong claimed result is reported as a mismatch")
    check("mismatch" not in (await agent.process("check: 2 + 3 = 5", {})).text.lower(),
          "A4-02: a correct claimed result is not reported")

    # A3: numbers and names are matched as whole tokens (RC-06)
    check(reflection.unsupported_claims("He was born in 643.", "Newton was born in 1643.") == ["643"],
          "A3-01: 643 is not supported by 1643")
    check(reflection.unsupported_claims("Newton was born in 1643.", "Newton was born in 1643.") == [],
          "A3-01: the same number is supported")
    check(reflection.unsupported_claims("Isaac Newtonian was great.", "Sir Isaac Newton was English.") == ["Isaac Newtonian"],
          "A3-02: a name that only contains the source name is not supported")

    # A5: two-letter terms take part in extraction
    sentences = search_tools.extract_sentences(
        "AI", [{"href": "https://a.example/x", "body": "AI is useful for many everyday tasks today."}])
    check(len(sentences) == 1, "A5-01: a two-letter term (AI) extracts its sentence")

    # A6: birth-year templates keep the year
    facts = wiki_parse.build_facts("X", "u", "{{Infobox person\n| birth_date = {{birth year|1643}}\n}}\n'''X''' was a person.")
    check(facts["infobox"].get("birth date") == "1643", "A6-01: a {{birth year}} template keeps 1643")

    # A7: python3 label; a builtin used as a value (aliasing) is flagged; a plain call is still flagged
    check(code_analysis.analyze("python3", "x = 1")["ok"] is True, "A7-01: the python3 fence label is accepted")
    aliased = {f["rule"] for f in code_analysis.analyze("python", "e = exec\ne('print(1)')")["findings"]}
    check("DANGEROUS_NAME" in aliased, "A7-02: an aliased exec is flagged")
    direct = {f["rule"] for f in code_analysis.analyze("python", "eval('1')")["findings"]}
    check("DANGEROUS_CALL" in direct, "A7-02: a direct eval call is still flagged as a call")

    # A7-03: pass is a valid statement in the simulator
    check(simulate.simulate("def f():\n    pass\n", None, [])["ok"] is True, "A7-03: pass runs in the simulator")

    # A8: a note cut at the storage limit says so
    st = _store()
    memory = MemoryVaultAgent(st, semantic.SemanticIndex(st))
    check("cut" in (await memory.process("note: " + "word " * 1200, {"user_id": "u"})).text.lower(),
          "A8-02: a note over the limit reports that it was cut")

    # A9: a note answers only the question it actually covers (RC-08)
    st = _store()
    idx = semantic.SemanticIndex(st)
    idx.add("u", "note", "Project Zeta launch date is 4 March 2031 in Pune")
    gk = GeneralKnowledgeAgent(SearchAgent(ApiGateway(), st), None, idx)
    unrelated = await gk.process("launch a rocket to Mars", {"user_id": "u"})
    check("your notes" not in str(unrelated.source or ""), "A9-01: an unrelated question is not answered from a shared word")
    related = await gk.process("when is project zeta launch", {"user_id": "u"})
    check("your notes" in str(related.source), "A9-01: a question the note covers is answered from it")

    # A10: a request is not a haiku to check (RC-09)
    check(AT.CreativeWriterAgent._meter_lines("Write a haiku about rain\nIt should be sad\nUse water words\nend") is None,
          "A10-01: a request that mentions haiku is not a haiku to check")
    check(AT.CreativeWriterAgent._meter_lines("Haiku\nold pond\na frog jumps in\nsplash") == ["old pond", "a frog jumps in", "splash"],
          "A10-01: a user-supplied haiku is still checked")
    check(prompts.detect_form("write a poem about haiku culture") == "poem", "A10-02: a poem about haiku is a poem")
    check(prompts.detect_form("write a haiku about rain") == "haiku", "A10-02: a haiku request is a haiku")

    # A16: speech keeps the words and the operators (RC-10)
    check("and c" in voice.markdown_to_speech("if a<b and c>d is true.")["plain"],
          "A16-01: 'a<b and c>d' keeps its words in the speech text")
    bold = voice.markdown_to_speech("<b>bold</b> text")["plain"]
    check("bold" in bold and "<b>" not in bold, "A16-01: a real HTML tag is still removed")
    spoken = voice.markdown_to_speech("2 + 2 = 4.")["plain"]
    check("equals" in spoken and "=" not in spoken, "A16-02: '=' is spoken as 'equals'")

    # A17: whitespace token is unset; the scheme is case-insensitive; /api is guarded (RC-11)
    check(gateway.resolve_token("   ")[1] is True, "A17-01: a whitespace-only token is unset, a random one is generated")
    policy = gateway.GuardPolicy("tok", clock=lambda: 0.0)
    check(policy.check("GET", "/api/health", "bearer tok", "c", now=0.0).allowed,
          "A17-02: a lowercase bearer scheme is accepted")
    check(policy.check("GET", "/api", None, "c", now=0.0).status == 401, "A17-03: '/api' without a trailing slash is guarded")

    # A18: one p95 definition; per-agent summaries keep their own samples (RC-12)
    values = list(range(1, 101))
    check(S._p95(values) == _percentile(values, 95), "A18-01: budgets and telemetry use the same p95")
    st2 = _store()
    for a in range(50):
        for _ in range(200):
            st2.log_call(f"Agent {a:02d}", True, 10.0)
    check(st2.agent_summary(window=200)["Agent 00"]["n"] == 200,
          "A18-02: the oldest of 50 agents keeps its 200 samples")

    # A20: MIME-style base64 with line breaks is a valid file (RC-14)
    wrapped = base64.encodebytes(_png(64, 64)).decode()
    result = await S.VisionMetadataAgent().process("x", {"image_base64": wrapped})
    check(result.ok, "A20-01: base64 with MIME line breaks is accepted")
