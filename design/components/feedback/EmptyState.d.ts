/** The nothing-here surface. */
export interface EmptyStateProps {
  /** Lucide icon name. */
  icon?: string;
  title?: React.ReactNode;
  children?: React.ReactNode;
  /** The equivalent CLI command, shown as a copyable code chip. */
  command?: string;
  action?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function EmptyState(props: EmptyStateProps): JSX.Element;
