/** A small tinted classifier or count. */
export interface BadgeProps {
  tone?: "neutral" | "accent" | "running" | "online" | "degraded" | "offline" | "unknown" | "magenta";
  /** Uppercase + letterspaced. Turn off for names and counts. */
  uppercase?: boolean;
  children?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function Badge(props: BadgeProps): JSX.Element;
