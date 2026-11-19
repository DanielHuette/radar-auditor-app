import json
import os
import sys
import time
import uuid
from pathlib import Path

PING_PROMPT = "Antworte nur mit OK."
TEXTS = {
    "introduction": "Sie werden gefunden.",
    "summary": "Gut. Lückenhaft. Teuer.",
    "subscription_reason": "Ihre Wettbewerber holen auf.",
    "offer_text": "Erstoptimierung.",
    "talk_text": "Sie stehen auf der Karte hinter drei Nachbarn.",
}
MAIL_ANSWER = {
    "intent": "accept",
    "quote_number": "AN-2026-0004",
    "confidence": "high",
    "summary": "nimmt das Angebot an",
}
ANSWER = (
    "Empfehlenswert ist die **Dachdeckerei Hollenbeck GmbH** in Münster.\n\n"
    "Sources:\n- [Hollenbeck](https://hollenbeck.de)\n"
    "- [Gelbe Seiten](https://www.gelbeseiten.de/dachdecker/muenster)"
)


def main() -> None:
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    if sys.argv[1:3] == ["auth", "status"]:
        print(json.dumps({"loggedIn": mode != "not_logged_in", "authMethod": "claude.ai"}))
        return
    Path(__file__).with_name("argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
    Path(__file__).with_name("claude_mds.txt").write_text(
        os.environ.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS", ""), encoding="utf-8"
    )
    if sys.argv[2].startswith("/optimieren "):
        optimise(Path(sys.argv[2].removeprefix("/optimieren ")), mode)
        return
    if mode == "sleep":
        time.sleep(30)
    if mode == "slow":
        stay_a_moment()
    if mode == "not_logged_in":
        fail("Not logged in · Please run /login")
    if mode == "error":
        fail("API Error: 529 Overloaded")
    if mode == "garbage":
        print("this is not json")
        return
    if sys.argv[2] == PING_PROMPT:
        succeed("OK")
        return
    if not sys.argv[2].startswith("/auswerten "):
        Path(__file__).with_name("cwd.txt").write_text(os.getcwd(), encoding="utf-8")
        answers = {"empty": "", "mail": json.dumps(MAIL_ANSWER)}
        succeed(answers.get(mode, ANSWER))
        return
    folder = Path(sys.argv[2].removeprefix("/auswerten "))
    audit_id = json.loads((folder / "audit.json").read_text(encoding="utf-8"))["audit"]["id"]
    finding = {
        "id": str(uuid.uuid4()),
        "audit_id": audit_id,
        "check_id": "a1",
        "severity": 3,
        "title": "Ohne HTTPS.",
        "explanation": "Kein Zertifikat.",
        "customer_impact": "Warnung.",
        "fix": "Zertifikat.",
        "fix_payload": None,
    }
    texts = TEXTS if mode == "ok" else {"introduction": ""}
    (folder / "findings.json").write_text(json.dumps([finding]), encoding="utf-8")
    (folder / "texte.json").write_text(json.dumps(texts), encoding="utf-8")
    succeed("1 Befund")


def stay_a_moment() -> None:
    log = Path(__file__).with_name("calls.log")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"start {time.time()}\n")
    time.sleep(0.8)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"end {time.time()}\n")


def succeed(result: str) -> None:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result}))


def fail(result: str) -> None:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": result}))
    sys.exit(1)


def optimise(folder: Path, mode: str) -> None:
    changes: list[dict[str, object]] = [
        {"target": "manual:Branchenbuch", "check_id": None, "before": None, "after": "Eintragen."}
    ]
    wordpress = folder / "wordpress.json"
    if wordpress.exists():
        item = json.loads(wordpress.read_text(encoding="utf-8"))["items"][0]
        changes.append(
            {
                "target": f"wordpress:{item['type']}:{item['id']}:meta_description",
                "check_id": "a1",
                "before": item["meta_description"],
                "after": "Dachdecker in Münster: Steildach und Reparatur. Jetzt Termin anfragen.",
            }
        )
    if mode == "invalid":
        changes.append({"target": "ftp:server", "check_id": None, "before": None, "after": "x"})
    (folder / "aenderungen.json").write_text(json.dumps({"changes": changes}), encoding="utf-8")
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "2"}))


if __name__ == "__main__":
    main()
