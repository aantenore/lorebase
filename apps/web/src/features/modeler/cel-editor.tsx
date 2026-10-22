import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import {
  bracketMatching,
  defaultHighlightStyle,
  StreamLanguage,
  syntaxHighlighting,
  type StringStream,
} from '@codemirror/language';
import { Annotation, Compartment, EditorState } from '@codemirror/state';
import { EditorView, keymap, placeholder as placeholderText } from '@codemirror/view';
import { useEffect, useRef } from 'react';
import { editorTheme } from './codemirror-theme';

const KEYWORDS = new Set(['true', 'false', 'null', 'in']);

/** Tokens of CEL for highlighting: literals, keywords, identifiers, operators. */
const cel = StreamLanguage.define<null>({
  name: 'cel',
  startState: () => null,
  token(stream: StringStream) {
    if (stream.eatSpace()) return null;
    const quote = stream.peek();
    if (quote === '"' || quote === "'") {
      stream.next();
      let escaped = false;
      for (let next = stream.next(); next !== undefined; next = stream.next()) {
        if (next === quote && !escaped) break;
        escaped = !escaped && next === '\\';
      }
      return 'string';
    }
    if (stream.match(/^-?\d+(\.\d+)?([eE][+-]?\d+)?u?/)) return 'number';
    if (stream.match(/^[A-Za-z_$][\w$]*/)) {
      return KEYWORDS.has(stream.current()) ? 'keyword' : 'variableName';
    }
    if (stream.match(/^(==|!=|<=|>=|&&|\|\||[-+*/%<>!?:])/)) return 'operator';
    stream.next();
    return 'punctuation';
  },
});

/** Marks edits that come from the `value` prop, so they are not reported back as changes. */
const External = Annotation.define<boolean>();

export interface CelEditorProps {
  value: string;
  onChange: (text: string) => void;
  label: string;
  /** Id of the hint or error text the editor is described by. */
  describedBy?: string | undefined;
  invalid?: boolean;
  placeholder?: string | undefined;
}

/**
 * A CEL expression on CodeMirror 6: highlighting, bracket matching, history. Tab is left to
 * the browser so keyboard users move on to the next field. `value` is the source of truth.
 */
export default function CelEditor({
  value,
  onChange,
  label,
  describedBy,
  invalid = false,
  placeholder,
}: CelEditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  });

  const attributes = useRef(new Compartment());
  const attributesOf = () => {
    const out: Record<string, string> = { 'aria-label': label };
    if (describedBy !== undefined) out['aria-describedby'] = describedBy;
    if (invalid) out['aria-invalid'] = 'true';
    return EditorView.contentAttributes.of(out);
  };

  useEffect(() => {
    if (host.current === null) return;
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          history(),
          bracketMatching(),
          cel,
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          keymap.of([...defaultKeymap, ...historyKeymap]),
          editorTheme,
          EditorView.lineWrapping,
          ...(placeholder === undefined ? [] : [placeholderText(placeholder)]),
          attributes.current.of(attributesOf()),
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
    // The editor is created once; later prop changes are applied below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    view.current?.dispatch({ effects: attributes.current.reconfigure(attributesOf()) });
    // `attributesOf` reads exactly these props.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [label, describedBy, invalid]);

  useEffect(() => {
    const editor = view.current;
    if (editor === null || editor.state.doc.toString() === value) return;
    editor.dispatch({
      changes: { from: 0, to: editor.state.doc.length, insert: value },
      annotations: External.of(true),
    });
  }, [value]);

  return <div ref={host} className="min-h-9" data-cel-editor="" />;
}
