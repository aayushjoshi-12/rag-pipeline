import type { SettingValue } from '../../types'

/** Guided "switch a service" flows: the settings that belong together, shown conditionally on the provider. */
export interface Flow {
  id: string
  title: string
  description: string
  /** `POST /settings/test/{service}`; null = nothing cheap and safe to test. */
  service: 'llm' | 'embeddings' | 'vector_store' | 'storage' | 'parser' | null
  /** The setting that selects the provider (its pending value drives which fields appear). */
  selector: string
  keys: (value: (key: string) => SettingValue) => string[]
  /** Shown under the flow title; where the consequence of switching is explained. */
  consequence: string
  docs: string
}

const keyFor: Record<string, string> = {
  openrouter: 'openrouter_api_key',
  openai: 'openai_api_key',
  anthropic: 'anthropic_api_key',
}

export const FLOWS: Flow[] = [
  {
    id: 'llm',
    title: 'Chat model (LLM)',
    description: 'Which provider and model write the answers.',
    service: 'llm',
    selector: 'llm_provider',
    keys: (v) => {
      const p = String(v('llm_provider'))
      return [
        'llm_provider',
        'default_llm_model',
        ...(p === 'openrouter' ? ['openrouter_base_url'] : []),
        ...(keyFor[p] ? [keyFor[p]] : []),
        'llm_max_tokens',
        'llm_price_input_per_mtok',
        'llm_price_output_per_mtok',
      ]
    },
    consequence:
      'Applies to the next question, no restart. Existing cached answers stay until their TTL.',
    docs: 'docs/configuration.md (LLM)',
  },
  {
    id: 'embeddings',
    title: 'Embeddings',
    description: 'Which provider and model turn text into vectors.',
    service: 'embeddings',
    selector: 'embedding_provider',
    keys: (v) => [
      'embedding_provider',
      'embedding_model',
      ...(v('embedding_provider') === 'ollama' ? ['ollama_base_url'] : []),
      'embedding_dimension',
      'embedding_price_per_mtok',
    ],
    consequence:
      'Switching provider, model or dimension needs a reindex: every stored chunk is re-embedded. Search is unavailable or partial while it runs. The OpenAI provider uses the OpenAI key from the chat-model section.',
    docs: 'docs/configuration.md (Embeddings)',
  },
  {
    id: 'vector_store',
    title: 'Vector store',
    description: 'Where vectors live, the distance metric and the index.',
    service: 'vector_store',
    selector: 'vector_store',
    keys: (v) =>
      v('vector_store') === 'pgvector'
        ? [
            'vector_store',
            'vector_metric',
            'vector_index',
            'hnsw_m',
            'hnsw_ef_construction',
            'hnsw_ef_search',
            'ivfflat_lists',
            'ivfflat_probes',
          ]
        : [
            'vector_store',
            'chroma_host',
            'chroma_port',
            'chroma_collection',
            'vector_metric',
            'hnsw_m',
            'hnsw_ef_construction',
            'hnsw_ef_search',
          ],
    consequence:
      'Changing the store, metric or index build parameters needs a reindex into the new store. Query-time knobs (ef_search, probes) apply immediately.',
    docs: 'docs/configuration.md (Vector store)',
  },
  {
    id: 'storage',
    title: 'File storage',
    description: 'Where new uploads are kept.',
    service: 'storage',
    selector: 'storage_backend',
    keys: (v) =>
      v('storage_backend') === 's3'
        ? [
            'storage_backend',
            's3_bucket',
            's3_region',
            's3_prefix',
            's3_endpoint_url',
            's3_access_key_id',
            's3_secret_access_key',
          ]
        : ['storage_backend'],
    consequence:
      'Applies to new uploads immediately. Documents already stored stay where they are; nothing is copied.',
    docs: 'docs/configuration.md (Storage)',
  },
  {
    id: 'parser',
    title: 'Document parser',
    description: 'How PDFs become text (vision model, OCR, or the PDF text layer).',
    service: 'parser',
    selector: 'parser_provider',
    keys: (v) => [
      'parser_provider',
      ...(v('parser_provider') === 'ocr' ? ['ocr_lang'] : ['parser_llm_model']),
      'pdf_text_source',
      'pdf_dpi',
    ],
    consequence:
      'Applies to documents ingested afterwards, no restart. The provider key is the one in the chat-model section (OpenRouter, OpenAI or Anthropic).',
    docs: 'docs/configuration.md (Document parsing)',
  },
]

/** Every key a flow can ever show, regardless of provider (so the generic list never repeats them). */
export const ALL_FLOW_KEYS: Set<string> = new Set([
  'llm_provider',
  'default_llm_model',
  'openrouter_base_url',
  'openrouter_api_key',
  'openai_api_key',
  'anthropic_api_key',
  'llm_max_tokens',
  'llm_price_input_per_mtok',
  'llm_price_output_per_mtok',
  'embedding_provider',
  'embedding_model',
  'ollama_base_url',
  'embedding_dimension',
  'embedding_price_per_mtok',
  'vector_store',
  'chroma_host',
  'chroma_port',
  'chroma_collection',
  'vector_metric',
  'vector_index',
  'hnsw_m',
  'hnsw_ef_construction',
  'hnsw_ef_search',
  'ivfflat_lists',
  'ivfflat_probes',
  'storage_backend',
  's3_bucket',
  's3_region',
  's3_prefix',
  's3_endpoint_url',
  's3_access_key_id',
  's3_secret_access_key',
  'parser_provider',
  'parser_llm_model',
  'ocr_lang',
  'pdf_text_source',
  'pdf_dpi',
])
