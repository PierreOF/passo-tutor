/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_TUTOR_GATEWAY?: 'mock' | 'http'
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
