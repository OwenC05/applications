// A responsive status request deliberately does not hash/load local model files.
export function modelReadinessNotice(status) {
  if (status.model_readiness === 'not_checked_in_request') {
    return 'Model readiness not checked here. Indexing and search validate the local cache.';
  }
  if (status.models_ready === true) return 'Models cached.';
  if (status.models_ready === false) return 'Model setup required.';
  return 'Model readiness unknown.';
}
