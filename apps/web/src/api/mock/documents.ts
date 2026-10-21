import type { KeeperApi } from '../client';
import { apiErrorFromProblem, notFound } from '../errors';
import type {
  DocumentDetail,
  DocumentInfo,
  DocumentUploaded,
  ExtractionJob,
} from '../extraction-types';
import { decodeCursor, encodeCursor } from './engine';
import { fakeDigest, type MockDocument } from './sample/documents';

/** What the mock Keeper accepts, the way `keeper.documents` configures the real one. */
export const MOCK_DOCUMENT_RULES = {
  maxBytes: 20_971_520,
  mediaTypes: {
    '.md': 'text/markdown',
    '.markdown': 'text/markdown',
    '.txt': 'text/plain',
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  } as Record<string, string>,
  urlHosts: ['documents.example.test'] as readonly string[],
  tiers: [0, 1, 2] as readonly number[],
};

export interface DocumentsMock {
  api: KeeperApi['documents'];
}

const rejected = (pointer: string, message: string, status = 422) =>
  apiErrorFromProblem({
    type: `urn:lorebase:problem:${status === 413 ? 'payload-too-large' : 'validation-failed'}`,
    title: status === 413 ? 'Payload too large' : 'Request validation failed',
    status,
    code: status === 413 ? 'payload-too-large' : 'validation-failed',
    detail: status === 413 ? message : 'One or more request members are invalid.',
    errors: [{ pointer, message, code: 'invalid' }],
  });

async function digestOf(file: File): Promise<string> {
  try {
    const bytes = await file.arrayBuffer();
    const hash = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, '0')).join('');
  } catch {
    return fakeDigest(`${file.name}:${file.size.toString()}`);
  }
}

async function textOf(file: File, mediaType: string): Promise<string | null> {
  if (!mediaType.startsWith('text/')) return null;
  try {
    return await file.text();
  } catch {
    return null;
  }
}

const infoOf = (detail: DocumentDetail): DocumentInfo => ({
  id: detail.id,
  filename: detail.filename,
  media_type: detail.media_type,
  sha256: detail.sha256,
  size: detail.size,
  text_sha256: detail.text_sha256,
  metadata: structuredClone(detail.metadata),
  created_at: detail.created_at,
  created_by: detail.created_by,
});

/**
 * The document library in memory. An upload stores the document and queues an extraction job
 * that moves to running and to succeeded on the next two reads of the document; the mock finds
 * nothing to propose in a new document, so the job ends without a Proposal. Plain text and
 * Markdown keep their text for the viewer; PDF and DOCX are not parsed.
 */
export function createDocumentsMock(
  known: (domain: string) => boolean,
  seed: Record<string, MockDocument[]>,
): DocumentsMock {
  const libraries = new Map(Object.entries(seed).map(([d, docs]) => [d, structuredClone(docs)]));
  const polls = new Map<string, number>();
  let counter = 100;

  const library = (domain: string): MockDocument[] => {
    if (!known(domain)) throw notFound(`Domain ${domain}`);
    let docs = libraries.get(domain);
    if (docs === undefined) {
      docs = [];
      libraries.set(domain, docs);
    }
    return docs;
  };

  const advance = (job: ExtractionJob) => {
    const seen = polls.get(job.id);
    if (seen === undefined || job.status === 'succeeded' || job.status === 'failed') return;
    polls.set(job.id, seen + 1);
    if (seen === 0) {
      job.status = 'running';
    } else {
      job.status = 'succeeded';
      job.finished_at = new Date().toISOString();
      job.stats = { chunks: 1, candidates: 0, items: 0 };
    }
  };

  const api: KeeperApi['documents'] = {
    list: (domain, params) => {
      const all = library(domain).map((d) => d.detail);
      const offset = decodeCursor(params?.cursor);
      const limit = params?.limit;
      const end = limit === undefined ? all.length : offset + limit;
      return Promise.resolve({
        items: all.slice(offset, end).map(infoOf),
        next_cursor: end < all.length ? encodeCursor(end) : null,
      });
    },

    get: (domain, id) => {
      const found = library(domain).find((d) => d.detail.id === id);
      if (found === undefined) return Promise.reject(notFound(`Document ${id}`));
      for (const job of found.detail.extraction_jobs) advance(job);
      return Promise.resolve(structuredClone(found.detail));
    },

    upload: async (domain, input) => {
      const docs = library(domain);
      const tiers = input.tiers;
      if (
        tiers !== undefined &&
        (tiers.length === 0 || tiers.some((t) => !MOCK_DOCUMENT_RULES.tiers.includes(t)))
      ) {
        throw rejected('/body/tiers', 'must be a non-empty list of tiers among 0, 1 and 2');
      }
      let filename: string;
      let size: number;
      const metadata: Record<string, unknown> = { source: 'upload' };
      if ('file' in input) {
        filename = input.file.name;
        size = input.file.size;
      } else {
        let url: URL;
        try {
          url = new URL(input.url);
        } catch {
          throw rejected('/body/url', 'not a valid URL');
        }
        if (url.protocol !== 'https:') throw rejected('/body/url', 'scheme must be one of https');
        if (!MOCK_DOCUMENT_RULES.urlHosts.includes(url.hostname)) {
          throw rejected('/body/url', 'the host is not in keeper.documents.url_hosts');
        }
        filename = input.filename ?? decodeURIComponent(url.pathname.split('/').pop() ?? '');
        size = 4096;
        metadata['source'] = 'url';
        metadata['url'] = input.url;
      }
      const suffix = filename.includes('.')
        ? filename.slice(filename.lastIndexOf('.')).toLowerCase()
        : '';
      const mediaType = MOCK_DOCUMENT_RULES.mediaTypes[suffix];
      if (mediaType === undefined) {
        const accepted = Object.keys(MOCK_DOCUMENT_RULES.mediaTypes).sort().join(', ');
        throw rejected(
          '/body/file',
          `unsupported file type ${suffix === '' ? '(none)' : suffix}; use ${accepted}`,
        );
      }
      if (size === 0) throw rejected('/body/file', 'the document is empty');
      if (size > MOCK_DOCUMENT_RULES.maxBytes) {
        throw rejected(
          '/body/file',
          `The document exceeds ${MOCK_DOCUMENT_RULES.maxBytes.toString()} bytes.`,
          413,
        );
      }
      let text: string | null = null;
      let sha: string;
      if ('file' in input) {
        sha = await digestOf(input.file);
        text = await textOf(input.file, mediaType);
      } else {
        sha = fakeDigest(input.url);
      }

      const existing = docs.find((d) => d.detail.sha256 === sha);
      if (existing !== undefined) {
        return {
          document: infoOf(existing.detail),
          duplicate: true,
          job_id: null,
        } satisfies DocumentUploaded;
      }
      counter += 1;
      const now = new Date().toISOString();
      const extract = input.extract ?? true;
      const jobs: ExtractionJob[] = extract
        ? [
            {
              id: `job_${counter.toString()}`,
              status: 'queued',
              model: (tiers ?? [0, 1]).includes(2) ? 'qwen2.5:7b-instruct' : 'rules',
              prompt_version: 'extract-v1',
              branch: null,
              proposal_id: null,
              created_at: now,
              finished_at: null,
              usage: {},
              stats: {},
              error: null,
            },
          ]
        : [];
      const detail: DocumentDetail = {
        id: `doc_${counter.toString()}`,
        filename,
        media_type: mediaType,
        sha256: sha,
        size,
        text_sha256: text === null ? null : fakeDigest(`text:${sha}`),
        metadata,
        created_at: now,
        created_by: 'usr_mock',
        extraction_jobs: jobs,
      };
      docs.unshift({ detail, text });
      for (const job of jobs) polls.set(job.id, 0);
      return { document: infoOf(detail), duplicate: false, job_id: extract ? counter : null };
    },

    text: (domain, id) => {
      const found = library(domain).find((d) => d.detail.id === id);
      if (found === undefined) return Promise.reject(notFound(`Document ${id}`));
      return Promise.resolve(found.text);
    },
  };
  return { api };
}
