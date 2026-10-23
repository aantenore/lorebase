import { useSchema } from '@/api/queries';
import type { DomainExample } from '@/api/types';
import { useScope } from '@/app/scope';
import { Button } from '@/ui/button';

/** The sample questions the Domain's schema declares (`domain.examples`); empty when none. */
export function useDomainExamples(): readonly DomainExample[] {
  const scope = useScope();
  const schema = useSchema(scope);
  return schema.data?.domain_info?.examples ?? [];
}

/**
 * The Domain's sample questions as buttons. Renders nothing when the Domain declares none:
 * the questions come from the schema, never from the app.
 */
export function ExampleQuestions({
  examples,
  label,
  disabled = false,
  onPick,
  className = '',
}: {
  examples: readonly DomainExample[];
  label: string;
  disabled?: boolean;
  onPick: (question: string) => void;
  className?: string;
}) {
  if (examples.length === 0) return null;
  return (
    <div role="group" aria-label={label} className={`flex flex-wrap gap-2 ${className}`}>
      {examples.map((example) => (
        <Button
          key={example.label}
          size="sm"
          disabled={disabled}
          title={example.question}
          onClick={() => {
            onPick(example.question);
          }}
        >
          {example.label}
        </Button>
      ))}
    </div>
  );
}
