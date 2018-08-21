from . import bbsr_workflow, bbsr_python, utils, single_cell, tfa, mi
import gc


class Single_Cell_BBSR_TFA_Workflow(bbsr_workflow.BBSRWorkflow):

    cluster_index = None

    def __init__(self):
        # Read in the normal data files from BBSRWorkflow & Workflow
        super(Single_Cell_BBSR_TFA_Workflow, self).__init__()

    def preprocess_data(self):
        # Run the normal workflow preprocessing to read in data
        super(bbsr_workflow.BBSRWorkflow, self).preprocess_data()

        # Cluster and bulk up single cells to cluster
        bulk, self.cluster_index = single_cell.initial_clustering(self.expression_matrix)
        bulk = bulk.apply(single_cell._library_size_normalizer, axis=0, raw=True)
        utils.Debug.vprint("Pseudobulk data matrix assembled [{}]".format(bulk.shape))

        # Calculate TFA and then break it back into single cells
        self.design = tfa.TFA(self.priors_data, bulk, bulk).compute_transcription_factor_activity()
        self.design = single_cell.declustering(self.design, self.cluster_index, columns=self.expression_matrix.columns)
        self.response = self.expression_matrix

    def run_bootstrap(self, X, Y, idx, bootstrap):
        utils.Debug.vprint('Calculating MI, Background MI, and CLR Matrix', level=1)

        boot_cluster_idx = self.cluster_index[bootstrap]
        X_bulk = single_cell.reclustering(X, boot_cluster_idx).apply(single_cell._library_size_normalizer,
                                                                     axis=0, raw=True)
        Y_bulk = single_cell.reclustering(Y, boot_cluster_idx).apply(single_cell._library_size_normalizer,
                                                                     axis=0, raw=True)

        # Calculate CLR & MI if we're proc 0 or get CLR & MI from the KVS if we're not
        if self.is_master():
            clr_mat, _ = mi.MIDriver(cores=self.cores).run(X_bulk, Y_bulk)
            self.kvs.put('mi %d' % idx, clr_mat)
        else:
            clr_mat = self.kvs.view('mi %d' % idx)

        # Trying to get ahead of some memory leaks
        X_bulk, Y_bulk, bootstrap, boot_cluster_idx = None, None, None, None
        gc.collect()

        utils.Debug.vprint('Calculating betas using BBSR', level=1)
        ownCheck = utils.ownCheck(self.kvs, self.rank, chunk=25)

        # Run the BBSR on this bootstrap
        X = single_cell.ss_df_norm(X)
        Y = single_cell.ss_df_norm(Y)
        betas, re_betas = bbsr_python.BBSR_runner().run(X, Y, clr_mat, self.priors_data, self.kvs, self.rank, ownCheck)

        # Clear the MI data off the KVS
        if self.is_master():
            _ = self.kvs.get('mi %d' % idx)

        # Trying to get ahead of some memory leaks
        X, Y, idx, clr_mat = None, None, None, None
        gc.collect()

        return betas, re_betas
