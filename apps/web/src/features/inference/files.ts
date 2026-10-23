import type { InferenceConfig } from '@/config/env';
import { inference as inferenceMessages } from '@/messages/inference';

export type UploadCaps = Pick<
  InferenceConfig,
  'maxFiles' | 'maxFileBytes' | 'maxTotalBytes' | 'fileSuffixes'
>;

export interface Rejected {
  name: string;
  reason: string;
}

export interface FileSelection {
  files: File[];
  rejected: Rejected[];
}

const UNITS = ['B', 'KiB', 'MiB', 'GiB'] as const;

export function formatBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || Number.isInteger(value) ? 0 : 1;
  return `${value.toFixed(digits)} ${UNITS[unit] ?? 'B'}`;
}

function suffixOf(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot < 0 ? '' : name.slice(dot).toLowerCase();
}

/**
 * Adds `incoming` files to the `current` selection under the caps. A file that breaks a cap is
 * left out and reported; the same name twice replaces nothing (the table name would clash).
 */
export function addFiles(current: File[], incoming: File[], caps: UploadCaps): FileSelection {
  const files = [...current];
  const rejected: Rejected[] = [];
  let total = files.reduce((sum, file) => sum + file.size, 0);
  for (const file of incoming) {
    const refuse = (reason: string) => rejected.push({ name: file.name, reason });
    if (!caps.fileSuffixes.includes(suffixOf(file.name))) {
      refuse(inferenceMessages.files.suffixes(caps.fileSuffixes.join(', ')));
    } else if (files.some((f) => f.name === file.name)) {
      refuse(inferenceMessages.files.duplicate);
    } else if (files.length >= caps.maxFiles) {
      refuse(inferenceMessages.files.tooMany(caps.maxFiles));
    } else if (file.size > caps.maxFileBytes) {
      refuse(
        inferenceMessages.files.tooLarge(formatBytes(caps.maxFileBytes), formatBytes(file.size)),
      );
    } else if (total + file.size > caps.maxTotalBytes) {
      refuse(inferenceMessages.files.tooLargeTogether(formatBytes(caps.maxTotalBytes)));
    } else {
      files.push(file);
      total += file.size;
    }
  }
  return { files, rejected };
}
