/**
 * A typed message with an actionable hint — the pixel form of the CLI error envelope.
 */
export interface BannerProps {
  tone?: "info" | "success" | "warn" | "danger";
  title?: React.ReactNode;
  /** The body sentence: what happened, in plain words. */
  children?: React.ReactNode;
  /** The fix, rendered after a monospace "help:" prefix — same shape as the CLI. */
  hint?: React.ReactNode;
  /** Machine error code, e.g. "DOCKER_UNREACHABLE". */
  code?: string;
  actions?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function Banner(props: BannerProps): JSX.Element;
