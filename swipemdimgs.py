#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]\n]*\]\(([^)\n]+)\)|<img\b[^>\n]*?\bsrc=(['\"])(.*?)\2[^>\n]*>",
    re.IGNORECASE,
)

KEEP = "keep"
REMOVE = "remove"
FLASH_MS = 110


@dataclass(frozen=True)
class ImageRef:
    number: int
    line_index: int
    line_no: int
    target: str
    display_target: str
    path: Path
    exists: bool
    safe_delete: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Swipe through Markdown images and delete selected docling artifacts at the end."
    )
    parser.add_argument("markdown_file", help="Markdown file to review")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only print the image references that would be reviewed",
    )
    return parser.parse_args()


def parse_markdown_target(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<"):
        close = target.find(">")
        if close != -1:
            return target[1:close].strip()
    return target.split()[0].strip("'\"")


def is_remote_or_data_target(target: str) -> bool:
    parsed = urlparse(target)
    return parsed.scheme in {"http", "https", "data", "mailto"}


def resolve_local_path(markdown_file: Path, target: str) -> Path | None:
    if is_remote_or_data_target(target):
        return None

    parsed = urlparse(target)
    if parsed.scheme and parsed.scheme != "file":
        return None

    if parsed.scheme == "file":
        local_target = unquote(parsed.path)
        return Path(local_target).expanduser()

    without_fragment = target.split("#", 1)[0].split("?", 1)[0]
    local_target = unquote(without_fragment)
    return (markdown_file.parent / local_target).expanduser()


def is_under(path: Path, parent: Path) -> bool:
    try:
        resolved_path = path.resolve(strict=False)
        resolved_parent = parent.resolve(strict=False)
    except OSError:
        return False
    return resolved_path == resolved_parent or resolved_parent in resolved_path.parents


def is_safe_artifact_path(markdown_file: Path, path: Path) -> bool:
    artifact_dirs = [
        markdown_file.parent / f"{markdown_file.stem}_artifacts",
        markdown_file.parent / f"{markdown_file.stem}_artefacts",
    ]
    return any(is_under(path, artifact_dir) for artifact_dir in artifact_dirs)


def read_markdown_images(markdown_file: Path) -> tuple[list[str], list[ImageRef]]:
    try:
        with markdown_file.open("r", encoding="utf-8", newline="") as handle:
            lines = handle.readlines()
    except OSError as exc:
        raise SystemExit(f"could not read {markdown_file}: {exc}") from exc

    refs: list[ImageRef] = []
    for line_index, line in enumerate(lines):
        for match in MARKDOWN_IMAGE_RE.finditer(line):
            raw_target = match.group(1) if match.group(1) is not None else match.group(3)
            target = parse_markdown_target(raw_target)
            path = resolve_local_path(markdown_file, target)
            if path is None:
                continue

            path = path.resolve(strict=False)
            try:
                display_target = str(path.relative_to(markdown_file.parent.resolve(strict=False)))
            except ValueError:
                display_target = str(path)

            refs.append(
                ImageRef(
                    number=len(refs) + 1,
                    line_index=line_index,
                    line_no=line_index + 1,
                    target=target,
                    display_target=display_target,
                    path=path,
                    exists=path.exists(),
                    safe_delete=is_safe_artifact_path(markdown_file, path),
                )
            )

    return lines, refs


def commit_changes(
    markdown_file: Path,
    lines: list[str],
    refs: list[ImageRef],
    decisions: list[str | None],
) -> tuple[int, int, list[str]]:
    remove_indexes = [index for index, decision in enumerate(decisions) if decision == REMOVE]
    if not remove_indexes:
        return 0, 0, []

    remove_line_indexes = {refs[index].line_index for index in remove_indexes}
    new_lines = [
        line for line_index, line in enumerate(lines) if line_index not in remove_line_indexes
    ]

    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{markdown_file.name}.",
            suffix=".tmp",
            dir=str(markdown_file.parent),
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(new_lines)
        os.replace(temp_name, markdown_file)
        temp_name = None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass

    refs_by_path: dict[Path, list[ImageRef]] = {}
    for ref in refs:
        refs_by_path.setdefault(ref.path, []).append(ref)

    deleted_files = 0
    errors: list[str] = []
    for path, path_refs in refs_by_path.items():
        if not any(ref.line_index not in remove_line_indexes for ref in path_refs):
            ref = path_refs[0]
            if not ref.safe_delete:
                errors.append(f"left file in place, not under artifact dir: {ref.display_target}")
                continue
            if not path.exists():
                continue
            try:
                path.unlink()
                deleted_files += 1
            except OSError as exc:
                errors.append(f"could not delete {ref.display_target}: {exc}")

    return len(remove_line_indexes), deleted_files, errors


def print_dry_run(markdown_file: Path, refs: list[ImageRef]) -> None:
    print(f"{markdown_file}: {len(refs)} local image reference(s)")
    for ref in refs:
        marker = "" if ref.exists else " [missing]"
        safety = "" if ref.safe_delete else " [outside artifact dir]"
        print(f"{ref.number:4d}  line {ref.line_no:4d}  {ref.display_target}{marker}{safety}")


def run_gui(markdown_file: Path, lines: list[str], refs: list[ImageRef]) -> int:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango
    except (ImportError, ValueError) as exc:
        print("swipemdimgs needs GTK 3, PyGObject, and GdkPixbuf installed.", file=sys.stderr)
        print(f"import error: {exc}", file=sys.stderr)
        return 1

    if Gdk.Display.get_default() is None:
        print("no graphical display is available for the review window", file=sys.stderr)
        return 1

    class SwipeWindow:
        def __init__(self) -> None:
            self.decisions: list[str | None] = [None] * len(refs)
            self.current = 0
            self.waiting = False
            self.finished = False
            self.exit_code = 0

            self.window = Gtk.Window(title=f"swipemdimgs - {markdown_file.name}")
            self.window.set_default_size(1100, 780)
            self.window.connect("destroy", self.quit)
            self.window.connect("key-press-event", self.on_key_press)

            self.background = Gtk.EventBox()
            self.window.add(self.background)

            self.outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.outer.set_border_width(10)
            self.background.add(self.outer)

            self.status = Gtk.Label()
            self.status.set_xalign(0)
            self.status.set_selectable(True)
            self.outer.pack_start(self.status, False, False, 0)

            self.image = Gtk.Image()
            self.image.set_halign(Gtk.Align.CENTER)
            self.image.set_valign(Gtk.Align.CENTER)
            self.image.set_hexpand(True)
            self.image.set_vexpand(True)
            self.outer.pack_start(self.image, True, True, 0)

            self.path_label = Gtk.Label()
            self.path_label.set_xalign(0)
            self.path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            self.path_label.set_selectable(True)
            self.outer.pack_start(self.path_label, False, False, 0)

            self.buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.outer.pack_start(self.buttons, False, False, 0)
            self.keep_button = self.add_button("k  keep", lambda _button: self.primary())
            self.remove_button = self.add_button("r  remove", lambda _button: self.choose(REMOVE))
            self.undo_button = self.add_button("u  undo", lambda _button: self.undo())
            self.cancel_button = self.add_button("Esc  cancel", lambda _button: self.quit())

            self.render_image()
            self.window.show_all()

        def add_button(self, label: str, callback) -> object:
            button = Gtk.Button(label=label)
            button.connect("clicked", callback)
            self.buttons.pack_start(button, False, False, 0)
            return button

        def max_image_size(self) -> tuple[int, int]:
            screen = Gdk.Screen.get_default()
            if screen is None:
                return 1000, 650
            return max(320, int(screen.get_width() * 0.82)), max(
                240, int(screen.get_height() * 0.70)
            )

        def scaled_pixbuf(self, path: Path) -> object:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
            width = pixbuf.get_width()
            height = pixbuf.get_height()
            max_width, max_height = self.max_image_size()
            scale = min(max_width / width, max_height / height)
            new_width = max(1, int(width * scale))
            new_height = max(1, int(height * scale))
            if new_width == width and new_height == height:
                return pixbuf
            return pixbuf.scale_simple(new_width, new_height, GdkPixbuf.InterpType.BILINEAR)

        def render_image(self) -> None:
            self.finished = False
            self.set_action_buttons(True)

            ref = refs[self.current]
            remove_count = sum(1 for decision in self.decisions if decision == REMOVE)
            self.status.set_text(
                f"{self.current + 1}/{len(refs)}   remove marked: {remove_count}   "
                "k keep   r remove   u undo"
            )
            warnings = []
            if not ref.exists:
                warnings.append("missing file")
            if not ref.safe_delete:
                warnings.append("outside artifact dir, file will not be deleted")
            suffix = f"  ({', '.join(warnings)})" if warnings else ""
            self.path_label.set_text(f"line {ref.line_no}: {ref.display_target}{suffix}")

            if not ref.exists:
                self.image.clear()
                return

            try:
                self.image.set_from_pixbuf(self.scaled_pixbuf(ref.path))
            except Exception as exc:  # noqa: BLE001 - GUI should keep moving on bad images.
                self.image.clear()
                self.path_label.set_text(f"line {ref.line_no}: {ref.display_target}  ({exc})")

        def render_summary(self) -> None:
            self.finished = True
            self.set_action_buttons(False)
            remove_count = sum(1 for decision in self.decisions if decision == REMOVE)
            self.status.set_text(
                f"Done. Remove {remove_count} of {len(refs)} image reference(s)?"
            )
            self.path_label.set_text("Enter/y applies changes. u goes back. Esc/q cancels.")
            self.image.clear()

        def render_applied(
            self, removed_lines: int, deleted_files: int, errors: list[str]
        ) -> None:
            self.finished = True
            self.waiting = True
            self.set_action_buttons(False)
            self.status.set_text(
                f"Applied. Removed {removed_lines} Markdown line(s), deleted {deleted_files} file(s)."
            )
            if errors:
                self.path_label.set_text("Warnings: " + " | ".join(errors))
            else:
                self.path_label.set_text("Close the window.")
            self.image.clear()

        def set_action_buttons(self, reviewing: bool) -> None:
            if reviewing:
                self.keep_button.set_label("k  keep")
                self.keep_button.set_sensitive(True)
            else:
                self.keep_button.set_label("Enter/y  apply")
                self.keep_button.set_sensitive(not self.waiting)
            self.remove_button.set_sensitive(reviewing)
            self.undo_button.set_sensitive(not self.waiting)
            self.cancel_button.set_sensitive(True)

        def primary(self) -> None:
            if self.finished:
                self.apply()
            else:
                self.choose(KEEP)

        def choose(self, decision: str) -> None:
            if self.waiting or self.finished:
                return
            color = "#226b3a" if decision == KEEP else "#8c2424"
            self.flash(color)
            self.waiting = True
            GLib.timeout_add(FLASH_MS, self.finish_choice, decision)

        def finish_choice(self, decision: str) -> bool:
            self.decisions[self.current] = decision
            self.current += 1
            self.waiting = False
            if self.current >= len(refs):
                self.render_summary()
            else:
                self.render_image()
            return False

        def undo(self) -> None:
            if self.waiting:
                return
            self.flash("#245e9b")
            self.waiting = True
            GLib.timeout_add(FLASH_MS, self.finish_undo)

        def finish_undo(self) -> bool:
            if self.current > 0:
                self.current -= 1
                self.decisions[self.current] = None
            self.waiting = False
            self.render_image()
            return False

        def apply(self) -> None:
            if self.waiting or not self.finished:
                return
            self.flash("#226b3a")
            try:
                removed_lines, deleted_files, errors = commit_changes(
                    markdown_file, lines, refs, self.decisions
                )
            except OSError as exc:
                self.status.set_text("Could not apply changes.")
                self.path_label.set_text(str(exc))
                return
            self.render_applied(removed_lines, deleted_files, errors)

        def flash(self, color_text: str) -> None:
            color = Gdk.RGBA()
            color.parse(color_text)
            self.background.override_background_color(Gtk.StateFlags.NORMAL, color)
            GLib.timeout_add(FLASH_MS, self.reset_background)

        def reset_background(self) -> bool:
            self.background.override_background_color(Gtk.StateFlags.NORMAL, None)
            return False

        def on_key_press(self, _window, event) -> bool:
            key = (Gdk.keyval_name(event.keyval) or "").lower()
            if key in {"escape", "q"}:
                self.quit()
                return True
            if self.finished:
                if key in {"return", "kp_enter", "y"}:
                    self.apply()
                    return True
                if key == "u":
                    self.undo()
                    return True
                return False
            if key == "k":
                self.choose(KEEP)
                return True
            if key == "r":
                self.choose(REMOVE)
                return True
            if key == "u":
                self.undo()
                return True
            return False

        def quit(self, *_args) -> None:
            Gtk.main_quit()

    app = SwipeWindow()
    Gtk.main()
    return app.exit_code


def main() -> int:
    args = parse_args()
    markdown_file = Path(args.markdown_file).expanduser().resolve(strict=False)
    if markdown_file.suffix.lower() != ".md":
        print("swipemdimgs only accepts .md files", file=sys.stderr)
        return 2
    if not markdown_file.exists():
        print(f"file not found: {markdown_file}", file=sys.stderr)
        return 2

    lines, refs = read_markdown_images(markdown_file)
    if args.dry_run:
        print_dry_run(markdown_file, refs)
        return 0
    if not refs:
        print(f"no local image references found in {markdown_file}")
        return 0
    return run_gui(markdown_file, lines, refs)


if __name__ == "__main__":
    raise SystemExit(main())
