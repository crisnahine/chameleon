export const Modal = (props: { open: boolean }) => {
  return props.open ? <div className="modal" /> : null;
};
