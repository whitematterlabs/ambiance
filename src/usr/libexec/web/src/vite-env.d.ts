/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Release build id this bundle was compiled into, stamped by pairelease
   * (e.g. "0.1.0+build.66"). Absent in dev / plain pnpm builds. */
  readonly VITE_PAI_BUILD?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
