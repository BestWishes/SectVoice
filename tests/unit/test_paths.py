from pathlib import Path

from sectvoice.paths import AppPaths


def test_explicit_root_is_relocatable(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "chosen-install"
    app = root / "app"
    app.mkdir(parents=True)
    monkeypatch.setenv("SECTVOICE_ROOT", str(root))
    monkeypatch.setenv("SECTVOICE_APP_DIR", str(app))

    paths = AppPaths.discover()

    assert paths.root == root.resolve()
    assert paths.source == app.resolve()
    assert paths.runtime == root.resolve() / "runtime"
    assert paths.data == root.resolve() / "data"
    assert "H:\\SectVoice" not in str(paths.root)


def test_source_checkout_remains_the_default(monkeypatch) -> None:
    monkeypatch.delenv("SECTVOICE_ROOT", raising=False)
    monkeypatch.delenv("SECTVOICE_APP_DIR", raising=False)

    paths = AppPaths.discover()

    checkout = Path(__file__).resolve().parents[2]
    assert paths.source == checkout
    expected_root = checkout.parent if checkout.name.lower() == "source" else checkout
    assert paths.root == expected_root
