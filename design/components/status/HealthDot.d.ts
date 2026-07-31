/** A bare health dot for dense rows. */
export interface HealthDotProps {
  status?: "running" | "online" | "degraded" | "offline" | "unknown";
  size?: number;
  title?: string;
  style?: React.CSSProperties;
}
export declare function HealthDot(props: HealthDotProps): JSX.Element;
