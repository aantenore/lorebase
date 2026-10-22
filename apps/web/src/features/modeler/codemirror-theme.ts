import { EditorView } from '@codemirror/view';

/** Colors and borders of the CodeMirror editors of the Modeler, from the app tokens. */
export const editorTheme = EditorView.theme({
  '&': {
    backgroundColor: 'var(--lb-surface)',
    color: 'var(--lb-text)',
    border: '1px solid var(--lb-line)',
    borderRadius: '0.375rem',
    fontSize: '0.8125rem',
    height: '100%',
  },
  '&.cm-focused': { outline: '2px solid var(--lb-ring)' },
  '.cm-scroller': { fontFamily: 'var(--font-mono)', overflow: 'auto' },
  '.cm-gutters': {
    backgroundColor: 'var(--lb-surface-2)',
    color: 'var(--lb-muted)',
    border: 'none',
  },
  '.cm-activeLine': { backgroundColor: 'var(--lb-surface-2)' },
  '.cm-activeLineGutter': { backgroundColor: 'var(--lb-surface-2)' },
  '.cm-cursor': { borderLeftColor: 'var(--lb-text)' },
});
