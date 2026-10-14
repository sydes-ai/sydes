"""Call the OpenAI API 3x with the flat context bundle, save raw responses.
Uses the OPENAI_API_KEY already configured in this environment -- the same
key `sydes verify-change --model openai:...` would use. Not wired into any
production path -- a one-off research script.
"""

import os
from pathlib import Path

from openai import OpenAI

MODEL = "gpt-5.1"

def main() -> None:
    context = (Path(__file__).parent / "context_bundle.md").read_text(encoding="utf-8")
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    for i in range(1, 4):
        print(f"--- run {i} ---")
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": context}],
        )
        text = resp.choices[0].message.content
        out_path = Path(__file__).parent / f"openai_run_{i}.md"
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote {out_path} ({len(text)} chars)")


if __name__ == "__main__":
    main()
