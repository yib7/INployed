import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

def find_latest_apply_data(root_dir: Path) -> Path | None:
    """Find the most recently created apply_data.json in the output directory."""
    candidates = list(root_dir.rglob("apply_data.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)

def copy_to_clipboard(text: str) -> None:
    """Copy text to Windows clipboard using clip.exe."""
    try:
        # clip.exe usually prefers utf-16le or local encoding.
        # utf-8 often works with modern Windows depending on chcp.
        subprocess.run("clip", input=text.encode("utf-16le"), check=True, shell=True)
        print("Copied to clipboard successfully via clip.exe.")
    except Exception as e:
        print(f"Failed to copy to clipboard: {e}")
        print("You can manually copy the prompt output below.")

def main():
    parser = argparse.ArgumentParser(description="Generate a prompt for the Claude Chrome extension using apply_data.json")
    parser.add_argument("json_path", nargs="?", help="Path to apply_data.json (default: auto-find latest in RESUME_TAILOR_OUTPUT)")
    parser.add_argument("--no-clip", action="store_true", help="Do not copy to clipboard, just print")
    args = parser.parse_args()

    json_path = args.json_path
    if not json_path:
        out_root_env = os.getenv("RESUME_TAILOR_OUTPUT", str(Path.home() / "Downloads" / "Generated_Resumes"))
        out_root = Path(out_root_env)
        if not out_root.exists():
            print(f"Error: Output directory {out_root} does not exist.")
            sys.exit(1)
        latest = find_latest_apply_data(out_root)
        if not latest:
            print(f"Error: Could not find any apply_data.json in {out_root}")
            sys.exit(1)
        json_path = latest
        print(f"Auto-selected latest apply_data: {json_path}")
    else:
        json_path = Path(json_path)
        if not json_path.exists():
            print(f"Error: File {json_path} does not exist.")
            sys.exit(1)

    with open(json_path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            print("Error: Invalid JSON file.")
            sys.exit(1)

    prompt = (
        "I am filling out a job application on this page. "
        "Please use the following applicant profile data to autofill the form fields. "
        "Match the fields to the best of your ability. If a required field is missing from the data, "
        "either deduce it if obvious, or ask me for it. Do NOT make up fake URLs or phone numbers.\n\n"
        "APPLICANT PROFILE DATA:\n"
        "```json\n"
        f"{json.dumps(data, indent=2)}\n"
        "```\n\n"
        "Please fill the form now."
    )

    print("\n" + "="*50)
    print("CLAUDE PROMPT:")
    print("="*50)
    print(prompt)
    print("="*50 + "\n")

    if not args.no_clip:
        copy_to_clipboard(prompt)

if __name__ == "__main__":
    main()
