/** One supervisord program inside a bench. */
export interface ProcessRowProps {
  /** Raw Procfile program key, e.g. "web", "worker_default", "schedule". */
  name: string;
  /** supervisord state, verbatim and uppercase. */
  state?: "RUNNING" | "STARTING" | "BACKOFF" | "STOPPED" | "FATAL" | "EXITED" | "UNKNOWN";
  /** null renders "pid —", never "pid 0". */
  pid?: number | null;
  uptime?: string;
  cpu?: number | null;
  rss?: string;
  selected?: boolean;
  onClick?: () => void;
  style?: React.CSSProperties;
}
export declare function ProcessRow(props: ProcessRowProps): JSX.Element;
/** The shared grid template; use it for any header row above ProcessRows. */
export declare const PROCESS_GRID: string;
