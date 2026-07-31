/** A bordered surface with an optional header row. */
export interface CardProps {
  title?: React.ReactNode;
  subtitle?: React.ReactNode;
  /** Header-right controls, usually IconButtons. */
  actions?: React.ReactNode;
  /** Footer row, right-aligned; use for Dialog-style confirm/cancel pairs. */
  footer?: React.ReactNode;
  /** Set false when the child manages its own insets (tables, log views). */
  padded?: boolean;
  tone?: "default" | "danger";
  children?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function Card(props: CardProps): JSX.Element;
