const { Dialog, Button, Input, Select, Checkbox, Banner, Spinner } = window.CaffeinatedWhaleDesignSystem_2d9598;

function NewInstanceDialog({ open, busy, tip, onClose, onCreate }) {
  return (
    <Dialog open={open} title="New instance" subtitle="cwcli init" onClose={busy ? undefined : onClose} width={470}
      footer={busy ? null : (
        <React.Fragment>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon="plus" onClick={onCreate}>Create instance</Button>
        </React.Fragment>
      )}>
      {busy ? (
        <Spinner label="Provisioning bench and site" tip={tip} />
      ) : (
        <React.Fragment>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-6)" }}>
            <Input label="Project name" mono placeholder="my-erp" autoFocus />
            <Select label="Frappe version" mono options={[{ value: 16, label: "16 (default)" }, { value: 15, label: "15" }, { value: 14, label: "14" }]} />
            <Input label="Site" mono placeholder="erp.localhost" />
            <Input label="Base port" mono placeholder="8000" hint="reserves 8000-8005 and 9000-9005" />
          </div>
          <Checkbox label="Open in VS Code when it is ready" hint="Installs the Dev Containers extension if it is missing." />
        </React.Fragment>
      )}
    </Dialog>
  );
}

function RemoveDialog({ open, project, volumes, onVolumes, onClose, onConfirm }) {
  return (
    <Dialog open={open} tone="danger" title="Remove this instance?" subtitle={project} onClose={onClose} width={470}
      footer={<React.Fragment>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="danger" icon="trash-2" onClick={onConfirm}>Remove instance</Button>
      </React.Fragment>}>
      <div>Containers, the project directory and every bench on it are deleted. A database backup is archived first unless you turn it off.</div>
      <Checkbox checked={volumes} onChange={onVolumes} label="Also remove named volumes" hint="This deletes the database. There is no undo." />
      <Banner tone="warn" hint={"cwcli rm " + project + " --yes --volumes"}>
        Destructive verbs are not part of the app's safe action set. The desktop app runs the same command you would type.
      </Banner>
    </Dialog>
  );
}

Object.assign(window, { NewInstanceDialog, RemoveDialog });
