/** A spinner with the product's rotating tip line. */
export interface SpinnerProps {
  label?: React.ReactNode;
  /** A rotating tip, taken from the CLI's own TIPS list. */
  tip?: React.ReactNode;
  size?: number;
  /** Renders as a single line for use inside a row or button. */
  inline?: boolean;
  style?: React.CSSProperties;
}
export declare function Spinner(props: SpinnerProps): JSX.Element;
