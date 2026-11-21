"""Edit lists per open STIL trace, with a live "edited" preview trace."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import threading

from .edit import EditError, PreviewMap, StilEditor, describe_char, edits_document, load_edits
from .load_stil import load_stil
from .model import Trace

_INDEX_FIELDS = ("after", "first", "last", "at")


@dataclass
class EditState:
    source_id: str
    source_trace: Trace
    editor: StilEditor
    edits: list[dict] = field(default_factory=list)
    preview_id: str | None = None
    preview_path: Path | None = None
    preview_pin_map: Path | None = None  # written when the signal set or names changed
    preview_map: PreviewMap | None = None
    notes: list[str] = field(default_factory=list)
    saved: dict | None = None


class EditManager:
    def __init__(self, session, work_dir: Path) -> None:  # type: ignore[no-untyped-def]
        self.session = session
        self.work_dir = work_dir
        self.states: dict[str, EditState] = {}
        self.lock = threading.Lock()

    # ---- lookup -------------------------------------------------------------
    def _state(self, trace_id: str) -> tuple[EditState, bool]:
        """(state, viewing_preview) for a source STIL trace id or its preview id."""

        for state in self.states.values():
            if state.preview_id == trace_id:
                return state, True
        if trace_id in self.states:
            return self.states[trace_id], False
        trace = self.session.get(trace_id)
        if trace.kind != "stil" or getattr(trace, "edit_of", None):
            raise EditError("only STIL sources can be edited (binaries are read-only)")
        stil = next(s for s in trace.sources if s["kind"] == "stil")
        pin_map = next((s["path"] for s in trace.sources if s["kind"] == "pin_map"), None)
        state = EditState(source_id=trace_id, source_trace=trace, editor=StilEditor(stil["path"], pin_map=pin_map))
        self.states[trace_id] = state
        return state, False

    def forget_missing(self) -> None:
        for source_id in list(self.states):
            if source_id not in self.session.traces:
                del self.states[source_id]

    # ---- operations ---------------------------------------------------------
    def add(self, trace_id: str, edit: dict) -> dict:
        with self.lock:
            state, on_preview = self._state(trace_id)
            edit = dict(edit)
            if on_preview:
                for key in _INDEX_FIELDS:
                    if edit.get(key) is not None:
                        index = state.preview_map.to_source(int(edit[key])) if state.preview_map else int(edit[key])
                        if index is None:
                            raise EditError(f"vector {edit[key]} was inserted by an edit; select an original vector")
                        edit[key] = index
            if edit.get("op") == "edge_time" and edit.get("first") is None and edit.get("last") is None \
                    and not edit.get("table"):
                edit["table"] = state.editor.vectors[int(edit.get("at", 0))].table
                edit.pop("at", None)
            self._rebuild(state, state.edits + [edit])
            return self.info_locked(state)

    def remove(self, trace_id: str, index: int) -> dict:
        with self.lock:
            state, _ = self._state(trace_id)
            if not 0 <= index < len(state.edits):
                raise EditError("no such edit")
            self._rebuild(state, state.edits[:index] + state.edits[index + 1:])
            return self.info_locked(state)

    def replace_all(self, trace_id: str, edits: list[dict]) -> dict:
        with self.lock:
            state, _ = self._state(trace_id)
            self._rebuild(state, list(edits))
            return self.info_locked(state)

    def info(self, trace_id: str) -> dict:
        """The edit state; for a STIL without edits it does not parse the STIL again (the first edit does)."""

        with self.lock:
            existing = next((s for s in self.states.values() if trace_id in (s.source_id, s.preview_id)), None)
            if existing is not None:
                return self.info_locked(existing)
            trace = self.session.get(trace_id)
            if trace.kind != "stil" or getattr(trace, "edit_of", None):
                return {"editable": False, "reason": "only STIL sources can be edited (binaries are read-only)"}
            stil = next(s for s in trace.sources if s["kind"] == "stil")
            return {"editable": True, "source_id": trace_id, "preview_id": None, "edits": [], "descriptions": [],
                    "source_name": Path(stil["path"]).name, "notes": [], "saved": None}

    @staticmethod
    def describe(state: EditState, edit: dict) -> str:
        vectors = state.editor.vectors

        def span(first, last=None) -> str:  # type: ignore[no-untyped-def]
            last = first if last is None else last
            lines = (vectors[first].line, vectors[last].line)
            if first == last:
                return f"#{first} (line {lines[0]})"
            return f"#{first}–{last} (lines {lines[0]}–{lines[1]})"

        op = edit.get("op")
        if op == "set_char":
            return f"{edit['signal']} → '{edit['char']}' for {span(int(edit['first']), edit.get('last'))}"
        if op == "rename_signal":
            return f"rename signal {edit['signal']} → {edit['to']}"
        if op == "add_signal":
            return f"add signal {edit['signal']} ({edit.get('direction', 'InOut')}, '{edit.get('fill', '')}' in every vector)"
        if op == "remove_signal":
            return f"remove signal {edit['signal']}"
        if op == "insert_wait":
            return f"wait {edit['duration']} after {span(int(edit['after']))}"
        if op == "set_period":
            return f"period {edit['period']} for {span(int(edit['first']), edit.get('last'))}"
        if op == "edge_time":
            where = (f"for {span(int(edit['first']), edit.get('last'))}" if edit.get("first") is not None
                     else f"in every vector using {edit.get('table')}")
            return f"{edit['signal']} '{edit['char']}' event {edit.get('event', 0)} → {edit['time']} {where}"
        return json.dumps(edit)

    def info_locked(self, state: EditState) -> dict:
        return {
            "editable": True,
            "source_id": state.source_id,
            "preview_id": state.preview_id,
            "edits": state.edits,
            "descriptions": [self.describe(state, e) for e in state.edits],
            "source_name": state.editor.source.name,
            "notes": state.notes,
            "saved": state.saved,
        }

    def save(self, trace_id: str, path: str | None = None) -> dict:
        with self.lock:
            state, _ = self._state(trace_id)
            if not state.edits or state.preview_path is None:
                raise EditError("nothing to save: add an edit first")
            source = state.editor.source
            origin = getattr(state.source_trace, "origin_path", None)
            if path:
                target = Path(path).expanduser()
            else:
                # Preprocessed input lives in the work dir: save next to the user's pattern file.
                base = origin if origin is not None else source
                suffix = ".pre.edited" if origin is not None else ".edited"
                target = base.with_name(base.stem + suffix + ".stil")
                n = 2
                while target.exists():
                    target = base.with_name(f"{base.stem}{suffix}-{n}.stil")
                    n += 1
            protected = {source.resolve()} | ({Path(origin).resolve()} if origin is not None else set())
            if target.resolve() in protected:
                raise EditError("refusing to overwrite the source STIL; choose another file name")
            edits_path = target.with_name(target.name[:-5] + ".edits.json" if target.name.endswith(".stil")
                                          else target.name + ".edits.json")
            target.write_text(state.preview_path.read_text())
            document = edits_document(state.editor, state.edits)
            preprocessed = getattr(state.source_trace, "preprocessed_from", None)
            if preprocessed:
                document["preprocess"] = preprocessed
            edits_path.write_text(json.dumps(document, indent=2) + "\n")
            state.saved = {"stil": str(target), "edits": str(edits_path)}
            if state.preview_pin_map is not None:
                pin_map = _sibling(target, ".pinmap.json")
                pin_map.write_text(state.preview_pin_map.read_text())
                state.saved["pin_map"] = str(pin_map)
            preview = self.session.traces.get(state.preview_id)
            if preview is not None:
                for item in preview.sources:
                    if item["kind"] == "stil":
                        item["path"] = str(target)
                    elif item["kind"] == "pin_map" and state.preview_pin_map is not None:
                        item["path"] = state.saved["pin_map"]
            return self.info_locked(state)

    def export(self, trace_id: str, path: str) -> dict:
        """Write the STIL shown (the edited copy, or the source as read, e.g. a preprocessed ATE file) to a file."""

        if not path:
            raise EditError("choose a file name")
        target = Path(path).expanduser()
        if not target.name.lower().endswith((".stil", ".stl")):
            target = target.with_name(target.name + ".stil")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            existing = next((s for s in self.states.values() if trace_id in (s.source_id, s.preview_id)), None)
        if existing is not None and existing.edits:
            return self.save(existing.source_id, str(target))
        trace = self.session.get(trace_id)
        if trace.kind != "stil":
            raise EditError("only a STIL can be exported as STIL")
        source = Path(next(s for s in trace.sources if s["kind"] == "stil")["path"])
        origin = getattr(trace, "origin_path", None)
        protected = {source.resolve()} | ({Path(origin).resolve()} if origin is not None else set())
        if target.resolve() in protected:
            raise EditError("refusing to overwrite the source STIL; choose another file name")
        target.write_bytes(source.read_bytes())
        result = {"stil": str(target)}
        pin_map = next((s["path"] for s in trace.sources if s["kind"] == "pin_map"), None)
        if pin_map:
            copy = _sibling(target, ".pinmap.json")
            if copy.resolve() != Path(pin_map).resolve():
                copy.write_bytes(Path(pin_map).read_bytes())
            result["pin_map"] = str(copy)
        return {"exported": result}

    def char_choices(self, trace_id: str, vector: int, signal: str) -> dict:
        """The characters a signal can take in a vector's WaveformTable (for the Change character form)."""

        with self.lock:
            state, on_preview = self._state(trace_id)
            if on_preview and state.preview_map is not None:
                source = state.preview_map.to_source(vector)
                if source is None:
                    raise EditError(f"vector {vector} was inserted by an edit; select an original vector")
                vector = source
            names = state.editor.current_names(state.edits if on_preview else [])
            if signal not in names:
                raise EditError(f"unknown signal {signal!r}")
            kind, value = names[signal]
            if kind == "added":
                table = state.editor.vectors[vector].table
                waveforms = state.editor._added_waveforms(value)[table]
                choices = [{"char": c, "kind": describe_char(e)[0], "text": describe_char(e)[1]} for c, e in waveforms.items()]
            else:
                choices = state.editor.char_choices(vector, value)
            info = state.editor.vectors[vector]
            direction = state.editor.directions.get(value, value if kind == "added" else "")
            return {"vector": vector, "table": info.table, "signal": signal, "direction": direction,
                    "editable": info.top_level, "choices": choices}

    def open_edit_list(self, path: Path) -> str:
        """Open the STIL named by an edit-list JSON and apply its edits; return the preview id."""

        document = load_edits(path)
        preprocess = document.get("preprocess")
        if preprocess:
            # The edited source was a preprocessed copy: regenerate it from the original files.
            inputs = [Path(preprocess["pattern"])] if preprocess["project"] == preprocess["pattern"] \
                else [Path(preprocess["project"]), Path(preprocess["pattern"])]
            missing = [str(p) for p in inputs if not p.is_file()]
            if missing:
                raise EditError(f"{path}: original files not found: {', '.join(missing)}")
            (source_id,) = self.session.load(inputs, replace=False)
        else:
            source = Path(document["source"]["path"])
            if not source.is_file():
                raise EditError(f"{path}: source STIL {source} not found")
            (source_id,) = self.session.load([source], replace=False)
        source = Path(document["source"]["path"])
        with self.lock:
            state, _ = self._state(source_id)
            if state.editor.sha256 != document["source"].get("sha256"):
                raise EditError(f"{source} changed since the edits were recorded (sha256 differs)")
            self._rebuild(state, list(document["edits"]))
            return state.preview_id or source_id

    # ---- internals ------------------------------------------------------------
    def _rebuild(self, state: EditState, edits: list[dict]) -> None:
        if not edits:
            self._drop_preview(state)
            state.edits = []
            state.notes = []
            state.saved = None
            return
        result = state.editor.apply(edits, state.source_trace)
        folder = self.work_dir / "edits"
        folder.mkdir(parents=True, exist_ok=True)
        preview_path = folder / f"{state.editor.source.stem}.edited.stil"
        preview_path.write_text(result.text)
        pin_map = state.editor.pin_map
        if result.pin_map is not None:
            pin_map = folder / f"{state.editor.source.stem}.edited.pinmap.json"
            pin_map.write_text(json.dumps(result.pin_map, indent=2) + "\n")
        try:
            preview = load_stil(preview_path, pin_map=pin_map)
        except Exception as exc:  # noqa: BLE001 - surface any front-end rejection to the user
            raise EditError(f"the edited STIL does not load: {exc}") from exc
        preview.title = f"{state.editor.source.stem}.edited.stil"
        preview.edit_of = state.source_id  # type: ignore[attr-defined]
        preview.notes = [f"edited copy of {state.editor.source.name}: {len(edits)} edit(s)"] + result.notes + preview.notes
        state.preview_map = PreviewMap(result.inserted_after, preview.vector_count)
        state.preview_pin_map = pin_map if result.pin_map is not None else None
        state.edits = edits
        state.notes = result.notes
        state.preview_path = preview_path
        state.saved = None
        preview_id = state.preview_id or f"edit:{state.source_id}"
        with self.session.lock:
            self.session.traces[preview_id] = preview
            if preview_id not in self.session.order:
                position = self.session.order.index(state.source_id) + 1 if state.source_id in self.session.order \
                    else len(self.session.order)
                self.session.order.insert(position, preview_id)
        state.preview_id = preview_id

    def _drop_preview(self, state: EditState) -> None:
        if state.preview_id is None:
            return
        with self.session.lock:
            self.session.traces.pop(state.preview_id, None)
            if state.preview_id in self.session.order:
                self.session.order.remove(state.preview_id)
        state.preview_id = None
        state.preview_path = None
        state.preview_pin_map = None
        state.preview_map = None

    def preview_mapping(self, trace_id: str) -> PreviewMap | None:
        for state in self.states.values():
            if state.preview_id == trace_id:
                return state.preview_map
        return None


def _sibling(stil: Path, suffix: str) -> Path:
    """``x.edited.stil`` -> ``x.edited<suffix>``."""

    name = stil.name[:-5] if stil.name.lower().endswith(".stil") else stil.name
    return stil.with_name(name + suffix)
