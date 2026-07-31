/**
 * A single-line text field.
 */
export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  /** Helper text below the field. */
  hint?: string;
  /** Replaces the hint and turns the border red. */
  error?: string;
  /** Lucide icon name shown inside the field. */
  icon?: string;
  /** Monospace the value — ports, paths, site names, tickets. */
  mono?: boolean;
  size?: "sm" | "md" | "lg";
  wrapperStyle?: React.CSSProperties;
}
export declare function Input(props: InputProps): JSX.Element;
