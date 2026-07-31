/** A modal. In this product a modal means a decision with consequences. */
export interface DialogProps {
  open?: boolean;
  title?: React.ReactNode;
  /** Monospace secondary line — the exact target, e.g. the project name. */
  subtitle?: React.ReactNode;
  tone?: "default" | "danger";
  onClose?: () => void;
  footer?: React.ReactNode;
  width?: number;
  children?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function Dialog(props: DialogProps): JSX.Element | null;
