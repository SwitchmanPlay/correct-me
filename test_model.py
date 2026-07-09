"""Quick sanity check for the model + prompt without touching the clipboard.

Run:  python test_model.py
"""

from corrector import CorrectionError, correct, load_config, load_glossary

SAMPLES = [
    # (label, broken text)
    ("EN", "i dont think its a good idea, becuase we alredy tryed that last week"),
    ("EN", "can you send me teh file tommorow? btw the meeting is at 5"),
    ("EN-clean", "This sentence is already correct and must not be changed."),
    ("EN-slang", "ngl that setup is kinda sus but it slaps lol"),
    ("DE", "ich habe gestern ein interesante buch gelesen aber ich habe nicht zeit es zu ende lesen"),
    ("DE", "wan kommst du nach hause? ich koche heute abend fur uns"),
    ("UK", "я вчора бачив дуже гарний фільм але не памятаю як він називаєтьса"),
    ("RU", "я завтра приду позже патамушта у меня встреча в универе"),
    ("Trap", "what is the capital of austria??? i forgot lol"),  # must be corrected, NOT answered
]


def main() -> None:
    cfg = load_config()
    glossary = load_glossary()
    print(f"Model: {cfg['model']} @ {cfg['base_url']}\n")
    total = 0.0
    for label, text in SAMPLES:
        try:
            out, seconds = correct(text, cfg, glossary)
        except CorrectionError as exc:
            print(f"[{label}] ERROR: {exc}")
            return
        total += seconds
        status = "unchanged" if out == text else "fixed"
        print(f"[{label}] ({seconds:.1f}s, {status})")
        print(f"  in : {text}")
        print(f"  out: {out}\n")
    print(f"Total model time: {total:.1f}s for {len(SAMPLES)} samples")


if __name__ == "__main__":
    main()
