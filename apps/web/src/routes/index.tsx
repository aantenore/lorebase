import { createFileRoute } from '@tanstack/react-router';

export const Route = createFileRoute('/')({
  component: Landing,
});

function Landing() {
  return (
    <main className="mx-auto max-w-xl p-8">
      <h1 className="text-xl font-semibold">Lorebase</h1>
      <p className="mt-2 text-muted">Ontology workspace. Select a Domain to begin.</p>
    </main>
  );
}
