/**
 * The health verdict token, rendered exactly as the CLI spells it.
 */
export interface StatusPillProps {
  /** One of the five tokens the fleet model can produce. Never invent a sixth. */
  status?: "running" | "online" | "degraded" | "offline" | "unknown";
  size?: "sm" | "md";
  showDot?: boolean;
  /** Overrides the visible text; the colour still comes from `status`. */
  label?: string;
  style?: React.CSSProperties;
}
export declare function StatusPill(props: StatusPillProps): JSX.Element;
export declare const STATUS_TOKENS: Record<string, { label: string; color: string; bg: string }>;
