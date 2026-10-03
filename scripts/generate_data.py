from dev import ROOT

from insight.scenarios import generate_all

if __name__ == "__main__":
    paths = generate_all(ROOT / "scenarios", ROOT / "data")
    for name, path in paths.items():
        print(f"{name}: {path.name}")
