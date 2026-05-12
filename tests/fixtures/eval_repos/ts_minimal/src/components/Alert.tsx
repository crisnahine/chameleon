export const Alert = (props: { level: string }) => {
  return <div className={`alert-${props.level}`} />;
};
