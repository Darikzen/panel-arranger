#!/usr/bin/env python3
"""
Panel Arranger — GNOME Shell top bar layout manager.
Move extension indicators and AppIndicator tray icons between left/center/right panel zones.

Requires: Python 3, GTK4, libadwaita, gnome-shell
Usage: python3 panel_arranger.py
"""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk


# ── Data Model ──────────────────────────────────────────────────────────────

POSITIONS = ("left", "center", "right")

@dataclass
class PanelItem:
    """Represents a single icon/indicator on the GNOME Shell top bar."""
    name: str                       # Human-readable display name
    item_id: str                    # Unique key (extension UUID or appindicator bus name)
    source: str                     # "extension" or "appindicator"
    current_pos: str                # left / center / right
    target_pos: str                 # desired position (edited by user)
    index: int = 0                  # ordering index within the box
    file_path: Optional[str] = None # JS file to patch
    line_pattern: Optional[str] = None  # Original addToStatusArea line
    is_session_mode: bool = False   # True if loaded as session extension (needs system edit)
    appindicator_match: str = ""    # substring to match in uniqueId for appindicator icons


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "panel-arranger"
CONFIG_FILE = CONFIG_DIR / "config.json"

APPINDICATOR_EXT_DIRS = [
    Path("/usr/share/gnome-shell/extensions/ubuntu-appindicators@ubuntu.com"),
    Path("/usr/share/gnome-shell/extensions/appindicatorsupport@rgcjonas.gmail.com"),
    Path.home() / ".local/share/gnome-shell/extensions/ubuntu-appindicators@ubuntu.com",
    Path.home() / ".local/share/gnome-shell/extensions/appindicatorsupport@rgcjonas.gmail.com",
]


# ── Scanner ─────────────────────────────────────────────────────────────────

def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def get_enabled_extensions() -> list[str]:
    r = run(["gnome-extensions", "list", "--enabled"])
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def get_extension_info(uuid: str) -> dict:
    r = run(["gnome-extensions", "show", uuid])
    info = {}
    for line in r.stdout.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            info[k.strip().lower()] = v.strip()
    return info


def find_extension_js(uuid: str) -> Optional[Path]:
    """Find the extension.js for a given UUID."""
    info = get_extension_info(uuid)
    path_str = info.get("path", "")
    if path_str:
        p = Path(path_str) / "extension.js"
        if p.exists():
            return p
    # Fallback search
    for base in [
        Path("/usr/share/gnome-shell/extensions"),
        Path.home() / ".local/share/gnome-shell/extensions",
    ]:
        p = base / uuid / "extension.js"
        if p.exists():
            return p
    return None


def parse_add_to_status_area(js_path: Path) -> Optional[dict]:
    """
    Parse addToStatusArea call to extract current position info.
    Returns dict with line_number, position, index, full_line.
    """
    text = js_path.read_text()
    # Match multi-line addToStatusArea calls
    # Patterns:
    #   Main.panel.addToStatusArea(id, indicator)
    #   Main.panel.addToStatusArea(id, indicator, index, 'position')
    pattern = re.compile(
        r"(Main\.panel\.addToStatusArea\s*\([^)]*\))",
        re.DOTALL
    )
    for match in pattern.finditer(text):
        call = match.group(1)
        # Count args
        # Extract position if present
        args_match = re.search(
            r"addToStatusArea\s*\(\s*"
            r"([^,]+),\s*"        # arg1: id
            r"([^,\)]+)"         # arg2: indicator
            r"(?:,\s*([^,\)]+))?" # arg3: index (optional)
            r"(?:,\s*([^,\)]+))?" # arg4: position (optional)
            r"\s*\)",
            call, re.DOTALL
        )
        if args_match:
            idx_arg = args_match.group(3)
            pos_arg = args_match.group(4)

            position = "right"  # default
            index = 0
            if pos_arg:
                pos_clean = pos_arg.strip().strip("'\"")
                if pos_clean in POSITIONS:
                    position = pos_clean
            if idx_arg:
                try:
                    index = int(idx_arg.strip())
                except ValueError:
                    index = 0

            line_num = text[:match.start()].count("\n") + 1
            return {
                "position": position,
                "index": index,
                "line_number": line_num,
                "full_match": call,
            }
    return None


def get_appindicator_icons() -> list[dict]:
    """Query D-Bus for registered StatusNotifierItems."""
    r = run([
        "gdbus", "call", "--session",
        "--dest", "org.kde.StatusNotifierWatcher",
        "--object-path", "/StatusNotifierWatcher",
        "--method", "org.freedesktop.DBus.Properties.GetAll",
        "org.kde.StatusNotifierWatcher",
    ])
    icons = []
    if r.returncode == 0:
        # Parse the registered items array
        match = re.search(r"\[([^\]]+)\]", r.stdout)
        if match:
            items_str = match.group(1)
            for item in re.findall(r"'([^']+)'", items_str):
                # Extract the last path component as the name
                parts = item.split("/")
                name = parts[-1] if parts else item
                # Skip generic ones
                if name in ("StatusNotifierItem",):
                    # Use bus name portion
                    bus_part = item.split("@")[0] if "@" in item else item
                    name = f"tray-{bus_part}"
                icons.append({"name": name, "bus_path": item})
    return icons


def find_appindicator_extension() -> Optional[Path]:
    """Find the active appindicator extension's indicatorStatusIcon.js."""
    for d in APPINDICATOR_EXT_DIRS:
        p = d / "indicatorStatusIcon.js"
        if p.exists():
            return p
    return None


def get_appindicator_tray_pos() -> str:
    """Get the current default tray position from gsettings."""
    r = run([
        "gsettings", "get",
        "org.gnome.shell.extensions.appindicator", "tray-pos",
    ])
    if r.returncode == 0:
        return r.stdout.strip().strip("'\"")
    return "right"


def scan_panel_items() -> list[PanelItem]:
    """Discover all panel items."""
    items = []
    seen_uuids = set()

    # 1) Scan GNOME Shell extensions
    for uuid in get_enabled_extensions():
        # Skip the appindicator extension itself — we handle its children separately
        if "appindicator" in uuid.lower():
            continue

        js_path = find_extension_js(uuid)
        if not js_path:
            continue

        parsed = parse_add_to_status_area(js_path)
        if not parsed:
            continue

        info = get_extension_info(uuid)
        display_name = info.get("name", uuid.split("@")[0])
        is_session = str(js_path).startswith("/usr/share")

        items.append(PanelItem(
            name=display_name,
            item_id=uuid,
            source="extension",
            current_pos=parsed["position"],
            target_pos=parsed["position"],
            index=parsed["index"],
            file_path=str(js_path),
            line_pattern=parsed["full_match"],
            is_session_mode=is_session,
        ))
        seen_uuids.add(uuid)

    # 2) Scan AppIndicator tray icons
    tray_pos = get_appindicator_tray_pos()
    ai_js = find_appindicator_extension()

    # Check if we already have custom LEFT_PANEL_ICONS patched
    existing_left_icons = []
    if ai_js:
        text = ai_js.read_text()
        m = re.search(r"LEFT_PANEL_ICONS\s*=\s*\[([^\]]*)\]", text)
        if m:
            existing_left_icons = [
                s.strip().strip("'\"")
                for s in m.group(1).split(",")
                if s.strip().strip("'\"")
            ]

    ai_icons = get_appindicator_icons()

    for icon_info in ai_icons:
        icon_name = icon_info["name"]
        # Determine current position
        pos = tray_pos
        if any(kw in icon_name.lower() for kw in existing_left_icons):
            pos = "left"

        items.append(PanelItem(
            name=icon_name.replace("_", " ").title(),
            item_id=f"appindicator:{icon_name}",
            source="appindicator",
            current_pos=pos,
            target_pos=pos,
            index=0,
            file_path=str(ai_js) if ai_js else None,
            appindicator_match=icon_name.lower(),
        ))

    # If the appindicator JS was patched but no icons appeared on D-Bus,
    # add a placeholder so the file shows up in restore_backups() and in the UI.
    if ai_js and not ai_icons:
        js_text = ai_js.read_text()
        if "// [panel-arranger] BEGIN" in js_text:
            items.append(PanelItem(
                name="AppIndicator (patched — icons not visible)",
                item_id="appindicator:__patched__",
                source="appindicator",
                current_pos=tray_pos,
                target_pos=tray_pos,
                index=0,
                file_path=str(ai_js),
                appindicator_match="",
            ))

    return items


# ── Patcher ─────────────────────────────────────────────────────────────────

def needs_sudo(path: str) -> bool:
    return path.startswith("/usr/")


def write_file_with_backup(path: str, content: str) -> bool:
    """Backup then write content to file — single pkexec prompt if root-owned."""
    bak = path + ".panel-arranger.bak"
    if needs_sudo(path):
        tmp = Path("/tmp/panel_arranger_patch.tmp")
        tmp.write_text(content)
        cmd = (
            f"cp {shlex.quote(path)} {shlex.quote(bak)} && "
            f"cp {shlex.quote(str(tmp))} {shlex.quote(path)}"
        )
        r = run(["pkexec", "bash", "-c", cmd])
        tmp.unlink(missing_ok=True)
        return r.returncode == 0
    else:
        shutil.copy2(path, bak)
        Path(path).write_text(content)
        return True


def patch_extension(item: PanelItem) -> bool:
    """Patch an extension's addToStatusArea call to use the target position."""
    if not item.file_path or not item.line_pattern:
        return False

    js_path = Path(item.file_path)
    text = js_path.read_text()

    old_call = item.line_pattern
    if old_call not in text:
        print(f"  [!] Could not find original pattern in {js_path}")
        return False

    # Rebuild the addToStatusArea call
    # Extract first two args from original
    args_match = re.search(
        r"addToStatusArea\s*\(\s*"
        r"([^,]+),\s*"
        r"([^,\)]+)",
        old_call, re.DOTALL
    )
    if not args_match:
        return False

    arg1 = args_match.group(1).strip()
    arg2 = args_match.group(2).strip()

    new_call = f"Main.panel.addToStatusArea({arg1}, {arg2}, {item.index}, '{item.target_pos}')"

    new_text = text.replace(old_call, new_call)

    return write_file_with_backup(item.file_path, new_text)


def patch_appindicator_icons(items: list[PanelItem]) -> bool:
    """
    Patch the appindicator extension to route specific icons to specific positions.
    Only handles items where source == "appindicator".
    """
    ai_items = [i for i in items if i.source == "appindicator"]
    if not ai_items:
        return True

    ai_js_path = find_appindicator_extension()
    if not ai_js_path:
        print("  [!] Cannot find appindicator extension JS")
        return False

    text = ai_js_path.read_text()

    # Collect icons that need non-default positioning
    default_pos = get_appindicator_tray_pos()
    left_icons = [i.appindicator_match for i in ai_items if i.target_pos == "left"]
    center_icons = [i.appindicator_match for i in ai_items if i.target_pos == "center"]
    # right is the default for most setups, so we only need overrides for left/center

    # Build the replacement addIconToPanel body.
    # Check for our marker block FIRST — if it exists, replace the whole block.
    # Checking for the raw addToStatusArea call second, because that call also
    # appears inside an already-patched block and would cause double-patching.
    patched_pattern = re.compile(
        r"([ \t]*)(// \[panel-arranger\] BEGIN.*?// \[panel-arranger\] END)",
        re.DOTALL
    )
    match = patched_pattern.search(text)
    if not match:
        add_pattern = re.compile(
            r"([ \t]*)(Main\.panel\.addToStatusArea\s*\(\s*indicatorId\s*,\s*statusIcon\s*[^;]*;)",
            re.DOTALL
        )
        match = add_pattern.search(text)
        if not match:
            print("  [!] Cannot find addToStatusArea call in appindicator extension")
            return False

    indent = match.group(1)
    old_block = match.group(2)

    def _js_array(lst):
        return "[" + ", ".join(f"'{s}'" for s in lst) + "]"

    lines = []
    lines.append("// [panel-arranger] BEGIN — auto-generated, do not edit manually")
    lines.append(f"const _paLeftIcons = {_js_array(left_icons)};")
    lines.append(f"const _paCenterIcons = {_js_array(center_icons)};")
    lines.append("const _paIconId = (statusIcon.uniqueId || '').toLowerCase();")
    lines.append("let _paPos = settings.get_string('tray-pos');")
    lines.append("let _paIdx = 1;")
    lines.append("if (_paLeftIcons.some(n => _paIconId.includes(n))) { _paPos = 'left'; _paIdx = 0; }")
    lines.append("else if (_paCenterIcons.some(n => _paIconId.includes(n))) { _paPos = 'center'; _paIdx = 0; }")
    lines.append("Main.panel.addToStatusArea(indicatorId, statusIcon, _paIdx, _paPos);")
    lines.append("// [panel-arranger] END")

    new_block = ("\n" + indent).join(lines)
    new_text = text.replace(old_block, new_block)

    return write_file_with_backup(str(ai_js_path), new_text)


def apply_changes(items: list[PanelItem]) -> tuple[bool, list[str]]:
    """Apply all pending changes. Returns (success, messages)."""
    messages = []
    ok = True

    # Group by type
    ext_items = [i for i in items if i.source == "extension" and i.target_pos != i.current_pos]
    ai_items = [i for i in items if i.source == "appindicator"]

    # Check if any appindicator item changed
    ai_changed = any(i.target_pos != i.current_pos for i in ai_items)

    for item in ext_items:
        messages.append(f"Patching {item.name}: {item.current_pos} → {item.target_pos}")
        if not patch_extension(item):
            messages.append(f"  ✗ Failed to patch {item.name}")
            ok = False
        else:
            messages.append(f"  ✓ Done")

    if ai_changed:
        messages.append("Patching AppIndicator extension for tray icon positions...")
        if not patch_appindicator_icons(ai_items):
            messages.append("  ✗ Failed to patch appindicator extension")
            ok = False
        else:
            messages.append("  ✓ Done")

    if ok and (ext_items or ai_changed):
        messages.append("")
        messages.append("Log out and back in to apply changes.")

    if not ext_items and not ai_changed:
        messages.append("No changes to apply.")

    # Save config
    save_config(items)

    return ok, messages


def save_config(items: list[PanelItem]):
    """Save current layout to config file for reference."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = []
    for item in items:
        data.append({
            "name": item.name,
            "item_id": item.item_id,
            "source": item.source,
            "target_pos": item.target_pos,
            "index": item.index,
        })
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def load_config() -> dict:
    """Load saved positions. Returns dict of item_id -> target_pos."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            return {d["item_id"]: d["target_pos"] for d in data}
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


# ── GUI ─────────────────────────────────────────────────────────────────────

class PanelItemRow(Gtk.Box):
    """A single draggable item row."""

    def __init__(self, item: PanelItem):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.item = item
        self.add_css_class("panel-item-row")
        self.set_margin_start(4)
        self.set_margin_end(4)
        self.set_margin_top(2)
        self.set_margin_bottom(2)

        # Icon indicating source type
        source_icon = "application-x-addon-symbolic" if item.source == "extension" else "view-pin-symbolic"
        icon = Gtk.Image.new_from_icon_name(source_icon)
        icon.set_pixel_size(16)
        icon.set_opacity(0.6)
        self.append(icon)

        # Name label
        label = Gtk.Label(label=item.name)
        label.set_hexpand(True)
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        self.append(label)

        # Sudo badge
        if item.file_path and needs_sudo(item.file_path):
            badge = Gtk.Label(label="sys")
            badge.add_css_class("dim-label")
            badge.add_css_class("caption")
            self.append(badge)


class PanelColumn(Gtk.Box):
    """One of the three panel zone columns (left / center / right)."""

    def __init__(self, position: str, app: "PanelArrangerApp"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.position = position
        self.app = app
        self.add_css_class("panel-column")

        # Header
        header = Gtk.Label(label=position.upper())
        header.add_css_class("heading")
        header.set_margin_top(8)
        header.set_margin_bottom(4)
        self.append(header)

        # Separator
        self.append(Gtk.Separator())

        # Scrollable list of items
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_box.add_css_class("boxed-list")
        self.list_box.set_vexpand(True)

        # Drop target
        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        drop.connect("drop", self._on_drop)
        self.list_box.add_controller(drop)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(self.list_box)
        self.append(scroll)

        # Placeholder
        self.placeholder = Gtk.Label(label="Drop items here")
        self.placeholder.set_opacity(0.3)
        self.placeholder.set_margin_top(24)
        self.list_box.set_placeholder(self.placeholder)

    def add_item(self, item: PanelItem):
        row = PanelItemRow(item)

        # Drag source
        drag = Gtk.DragSource.new()
        drag.set_actions(Gdk.DragAction.MOVE)
        drag.connect("prepare", self._on_drag_prepare, item)
        drag.connect("drag-begin", self._on_drag_begin, row)
        row.add_controller(drag)

        self.list_box.append(row)

    def clear(self):
        while True:
            row = self.list_box.get_row_at_index(0)
            if row is None:
                break
            self.list_box.remove(row)

    def _on_drag_prepare(self, source, x, y, item):
        val = GObject.Value()
        val.init(GObject.TYPE_STRING)
        val.set_string(item.item_id)
        return Gdk.ContentProvider.new_for_value(val)

    def _on_drag_begin(self, source, drag, row):
        icon = Gtk.DragIcon.get_for_drag(drag)
        label = Gtk.Label(label=row.item.name)
        label.add_css_class("title-4")
        icon.set_child(label)

    def _on_drop(self, target, value, x, y):
        item_id = value
        self.app.move_item(item_id, self.position)
        return True



class PanelArrangerWindow(Adw.ApplicationWindow):
    def __init__(self, app, items: list[PanelItem]):
        super().__init__(application=app, title="Panel Arranger")
        self.set_default_size(720, 480)
        self.items = items
        self.pa_app = app

        # Main layout
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # Header bar
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        # Refresh button
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan panel items")
        self.refresh_btn.connect("clicked", self._on_refresh)
        header.pack_start(self.refresh_btn)

        # Spinner (shown during scan)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner)

        # Apply button
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._on_apply)
        header.pack_end(apply_btn)

        # Restore button
        restore_btn = Gtk.Button(
            icon_name="edit-undo-symbolic",
            tooltip_text="Restore backups (created automatically when you Apply changes)",
        )
        restore_btn.connect("clicked", self._on_restore)
        header.pack_end(restore_btn)

        # Content: three columns
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toolbar_view.set_content(content)

        # Info bar
        info_label = Gtk.Label(
            label="Drag items between columns to rearrange your top bar. System files require authentication.",
        )
        info_label.set_wrap(True)
        info_label.set_margin_top(8)
        info_label.set_margin_bottom(8)
        info_label.set_margin_start(12)
        info_label.set_margin_end(12)
        info_label.add_css_class("dim-label")
        content.append(info_label)

        content.append(Gtk.Separator())

        columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1, homogeneous=True)
        columns_box.set_vexpand(True)
        content.append(columns_box)

        self.columns: dict[str, PanelColumn] = {}
        for pos in POSITIONS:
            col = PanelColumn(pos, self.pa_app)
            self.columns[pos] = col
            columns_box.append(col)

        self._populate()

    def _populate(self):
        for col in self.columns.values():
            col.clear()
        for item in self.items:
            col = self.columns.get(item.target_pos, self.columns["right"])
            col.add_item(item)

    def set_scanning(self, is_scanning: bool):
        self.refresh_btn.set_sensitive(not is_scanning)
        if is_scanning:
            self.spinner.start()
        else:
            self.spinner.stop()

    def refresh(self, new_items: list[PanelItem]):
        self.items = new_items
        self._populate()

    def _on_refresh(self, btn):
        self.pa_app.rescan()

    def _on_apply(self, btn):
        self.pa_app.apply()

    def _on_restore(self, btn):
        self.pa_app.restore_backups()


class PanelArrangerApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.github.panel-arranger",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.items: list[PanelItem] = []
        self.window: Optional[PanelArrangerWindow] = None

    def do_activate(self):
        if self.window:
            self.window.present()
            return

        # Load custom CSS
        css = Gtk.CssProvider()
        css.load_from_string("""
            .panel-column {
                background: alpha(@window_fg_color, 0.03);
                border-right: 1px solid alpha(@window_fg_color, 0.08);
            }
            .panel-item-row {
                padding: 8px 12px;
                border-radius: 8px;
            }
            .panel-item-row:hover {
                background: alpha(@accent_bg_color, 0.1);
            }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self.window = PanelArrangerWindow(self, [])
        self.window.present()
        self._start_scan(self._on_initial_scan_done)

    def _start_scan(self, on_done):
        """Run scan_panel_items() in a background thread, call on_done(items) on the main thread."""
        if self.window:
            self.window.set_scanning(True)

        def _worker():
            items = scan_panel_items()
            GLib.idle_add(on_done, items)

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_saved_config(self, items: list[PanelItem]):
        saved = load_config()
        for item in items:
            if item.item_id in saved:
                item.target_pos = saved[item.item_id]

    def _on_initial_scan_done(self, items: list[PanelItem]):
        self._apply_saved_config(items)
        self.items = items
        if self.window:
            self.window.set_scanning(False)
            self.window.refresh(self.items)
        return False  # remove GLib.idle_add source

    def move_item(self, item_id: str, new_pos: str):
        for item in self.items:
            if item.item_id == item_id:
                item.target_pos = new_pos
                break
        if self.window:
            self.window.refresh(self.items)

    def rescan(self):
        self._start_scan(self._on_rescan_done)

    def _on_rescan_done(self, items: list[PanelItem]):
        self._apply_saved_config(items)
        self.items = items
        if self.window:
            self.window.set_scanning(False)
            self.window.refresh(self.items)
        return False  # remove GLib.idle_add source

    def apply(self):
        ok, messages = apply_changes(self.items)

        dialog = Adw.AlertDialog(
            heading="Changes Applied" if ok else "Some Changes Failed",
            body="\n".join(messages),
        )
        dialog.add_response("ok", "OK")
        dialog.present(self.window)

        if ok:
            # Update current_pos to match target
            for item in self.items:
                item.current_pos = item.target_pos
            if self.window:
                self.window.refresh(self.items)

    def restore_backups(self):
        """Restore all .panel-arranger.bak files."""
        restored = []
        failed = []
        seen_paths = set()

        # Build the list of paths to check: all items + always the appindicator JS
        paths_to_check: list[tuple[str, str]] = []  # (file_path, display_name)
        for item in self.items:
            if item.file_path and item.file_path not in seen_paths:
                seen_paths.add(item.file_path)
                paths_to_check.append((item.file_path, item.name))

        ai_js = find_appindicator_extension()
        if ai_js and str(ai_js) not in seen_paths:
            seen_paths.add(str(ai_js))
            paths_to_check.append((str(ai_js), "AppIndicator extension"))

        for file_path, display_name in paths_to_check:
            bak = file_path + ".panel-arranger.bak"
            if needs_sudo(file_path):
                # exit 2 = backup not found (skip), other non-zero = real failure
                cmd = (
                    f"if test -f {shlex.quote(bak)}; then "
                    f"cp {shlex.quote(bak)} {shlex.quote(file_path)}; "
                    f"else exit 2; fi"
                )
                r = run(["pkexec", "bash", "-c", cmd])
                if r.returncode == 0:
                    restored.append(display_name)
                elif r.returncode != 2:
                    failed.append(display_name)
                # exit 2 → no backup, silently skip
            else:
                if not Path(bak).exists():
                    continue
                shutil.copy2(bak, file_path)
                restored.append(display_name)

        if restored:
            msg = "Restored backups for:\n" + "\n".join(f"  • {n}" for n in restored)
            msg += "\n\nLog out and back in to apply."
        else:
            msg = "No backup files found."

        if failed:
            msg += "\n\nFailed to restore:\n" + "\n".join(f"  • {n}" for n in failed)

        dialog = Adw.AlertDialog(heading="Restore Backups", body=msg)
        dialog.add_response("ok", "OK")
        dialog.present(self.window)


def main():
    app = PanelArrangerApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
