/** A native select in the system's chrome. */
export interface SelectOption { value: string | number; label: string }
export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  label?: string;
  hint?: string;
  options?: SelectOption[];
  size?: "sm" | "md";
  mono?: boolean;
  wrapperStyle?: React.CSSProperties;
}
export declare function Select(props: SelectProps): JSX.Element;
