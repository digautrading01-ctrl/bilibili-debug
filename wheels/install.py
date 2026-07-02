import os
import subprocess
import sys
from pathlib import Path

# Below is a Python script that iterates over a pre‑defined list of directories, 
# changes into each one, and runs python -m pip install -r requirements.txt. 
# The script uses the same Python interpreter that launched it (sys.executable) and 
# resolves relative paths against the script’s own location, so it works regardless of 
# where it is called from.

def main():
    # ==============================================
    #  EDIT THIS LIST with your target directories
    #  (absolute paths or relative to this script)
    # ==============================================
    directories = [
        "flask-3.0.3-py3-none-any.whl",
        "yt_dlp-2026.3.17-py3-none-any.whl",
        "browser_cookie3-0.20.1-py3-none-any.whl"
    ]

    if not directories:
        print("No directories specified. Edit the 'directories' list inside the script.")
        return

    # Resolve relative paths based on the script's own directory
    script_dir = Path(__file__).resolve().parent
    original_cwd = os.getcwd()

    for dir_path in directories:
        path = Path(dir_path)
        if not path.is_absolute():
            path = script_dir / path
        target_dir = path.resolve()

        print(f"\n--- Processing: {target_dir} ---")

        if not target_dir.is_dir():
            print(f"ERROR: Directory does not exist: {target_dir}. Skipping.")
            continue

        # Move into the target directory
        try:
            os.chdir(target_dir)
        except Exception as e:
            print(f"ERROR: Failed to change directory to {target_dir}: {e}")
            continue

        # Run pip install using the same Python interpreter
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                check=True,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            print(f"Successfully installed requirements in {target_dir}")
        except subprocess.CalledProcessError as e:
            print(f"ERROR: pip install failed in {target_dir} (exit code {e.returncode})")
        except Exception as e:
            print(f"ERROR: Unexpected error in {target_dir}: {e}")

    # Restore the original working directory
    os.chdir(original_cwd)
    print("\nAll directories processed.")

if __name__ == "__main__":
    main()