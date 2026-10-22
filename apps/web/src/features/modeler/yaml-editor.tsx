import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { yaml } from '@codemirror/lang-yaml';
import { defaultHighlightStyle, indentOnInput, syntaxHighlighting } from '@codemirror/language';
import { Annotation, EditorState } from '@codemirror/state';
import { EditorView, highlightActiveLine, keymap, lineNumbers } from '@codemirror/view';
import { useEffect, useRef } from 'react';
import { editorTheme } from './codemirror-theme';

export interface Jump {
  line: number;
  col: number;
  /** Changes on every request, so asking for the same place twice still moves the cursor. */
  nonce: number;
}

export interface YamlEditorProps {
  value: string;
  onChange: (text: string) => void;
  label: string;
  jump: Jump | null;
  /** A preview: the text cannot be edited from the keyboard. */
  readOnly?: boolean;
  /** Height classes of the host; 24rem by default. */
  className?: string;
}

/** Marks edits that come from the `value` prop, so they are not reported back as changes. */
const External = Annotation.define<boolean>();

/**
 * YAML editor on CodeMirror 6: YAML language, history, line numbers. `value` is the source
 * of truth: when it changes from outside (the form edited the document) the text is
 * replaced; typing reports the new text through `onChange`.
 */
export default function YamlEditor({
  value,
  onChange,
  label,
  jump,
  readOnly = false,
  className = 'h-96',
}: YamlEditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  });

  useEffect(() => {
    if (host.current === null) return;
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          history(),
          indentOnInput(),
          highlightActiveLine(),
          yaml(),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          keymap.of([...defaultKeymap, ...historyKeymap]),
          editorTheme,
          EditorState.readOnly.of(readOnly),
          EditorView.editable.of(!readOnly),
          EditorView.contentAttributes.of({ 'aria-label': label, 'aria-multiline': 'true' }),
          EditorView.updateListener.of((update) => {
            if (!update.docChanged) return;
            if (update.transactions.some((t) => t.annotation(External) === true)) return;
            onChangeRef.current(update.state.doc.toString());
          }),
        ],
      }),
    });
    view.current = editor;
    return () => {
      editor.destroy();
      view.current = null;
    };
    // The editor is created once; later `value` and `label` changes are applied below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const editor = view.current;
    if (editor === null || editor.state.doc.toString() === value) return;
    editor.dispatch({
      changes: { from: 0, to: editor.state.doc.length, insert: value },
      annotations: External.of(true),
    });
  }, [value]);

  useEffect(() => {
    const editor = view.current;
    if (editor === null || jump === null) return;
    const line = editor.state.doc.line(Math.min(Math.max(jump.line, 1), editor.state.doc.lines));
    const position = Math.min(line.from + Math.max(jump.col - 1, 0), line.to);
    editor.dispatch({ selection: { anchor: position }, scrollIntoView: true });
    editor.focus();
  }, [jump]);

  return (
    <div
      ref={host}
      role="group"
      aria-label={`${label} editor`}
      className={`${className} min-h-0`}
    />
  );
}
