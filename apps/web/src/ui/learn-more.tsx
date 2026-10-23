import { useOptionalConfig } from '@/config/context';
import { common as commonMessages } from '@/messages/common';
import { cn } from './cn';

/**
 * A small link into the documentation site (`docsUrl`), for explanations that do not belong
 * on the screen. Renders nothing when no documentation site is configured.
 */
export function LearnMore({ path, className }: { path: string; className?: string }) {
  const docsUrl = useOptionalConfig()?.docsUrl ?? null;
  if (docsUrl === null) return null;
  return (
    <a
      href={`${docsUrl}/${path.replace(/^\/+/, '')}`}
      target="_blank"
      rel="noreferrer"
      className={cn('whitespace-nowrap text-accent hover:underline', className)}
    >
      {commonMessages.learnMore}
    </a>
  );
}
