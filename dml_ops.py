# import tensorflow as tf
import torch
import torch.nn.functional as F
from sklearn.cluster import KMeans
import numpy as np

class DMLLoss():

    def __init__(self, labels, k, m, d):

        self.unique_classes = np.unique(labels)
        self.num_classes = self.unique_classes.shape[0]
        self.labels = labels

        self.k = k
        self.m = m
        self.d = d

        self.centroids = None
        self.assignments = np.zeros_like(labels, int)
        self.cluster_assignments = {}
        self.cluster_classes = np.repeat(range(self.num_classes), k)
        self.example_losses = None
        self.cluster_losses = None
        self.has_loss = None

    def update_clusters(self, rep_data, max_iter=20):
        """
        Given an array of representations for the entire training set,
        recompute clusters and store example cluster assignments in a
        quickly sampleable form.
        """
        # Lazily allocate tensor for centroids
        if self.centroids is None:
            self.centroids = torch.zeros(self.num_classes * self.k, rep_data.shape[1], requires_grad=True).cuda()

        for c in range(self.num_classes):
            class_mask = self.labels == self.unique_classes[c]  # build true/false mask for classes to allow us to extract them
            class_examples = rep_data[class_mask]  # extract the embds for this class
            kmeans = KMeans(n_clusters=self.k, init='k-means++', n_init=1, max_iter=max_iter)
            kmeans.fit(class_examples)  # run kmeans putting k clusters per class

            # Save cluster centroids for finding impostor clusters
            start = self.get_cluster_ind(c, 0)
            stop = self.get_cluster_ind(c, self.k)
            self.centroids[start:stop] = torch.from_numpy(kmeans.cluster_centers_)

            # Update assignments with new global cluster indexes (ie each sample in dataset belongs to cluster id x)
            self.assignments[class_mask] = self.get_cluster_ind(c, kmeans.predict(class_examples))

        # Construct a map from cluster to example indexes for fast batch creation (the opposite of assignmnets)
        for cluster in range(self.k * self.num_classes):
            cluster_mask = self.assignments == cluster
            self.cluster_assignments[cluster] = np.flatnonzero(cluster_mask)

    def update_losses(self, indexes, losses):
        """
        Given a list of examples indexes and corresponding losses
        store the new losses and update corresponding cluster losses.
        """
        # Lazily allocate structures for losses
        if self.example_losses is None:
            self.example_losses = np.zeros_like(self.labels, float)
            self.cluster_losses = np.zeros([self.k * self.num_classes], float)
            self.has_loss = np.zeros_like(self.labels, bool)

        # Update example losses
        indexes = np.array(indexes)  # these are sample indexs in the dataset, relating to the batch this is called on
        self.example_losses[indexes] = losses  # add loss for each of the examples in the batch
        self.has_loss[indexes] = losses  # set booleans

        # Find affected clusters and update the corresponding cluster losses
        clusters = np.unique(self.assignments[indexes])  # what clusters are these batch samples a part of?
        for cluster in clusters:  # for each cluster
            cluster_inds = self.assignments == cluster  # what samples are in this cluster?
            cluster_example_losses = self.example_losses[cluster_inds]  # for the samples in this cluster what are their losses

            # Take the average loss in the cluster of examples for which we have measured a loss
            if len(cluster_example_losses[self.has_loss[cluster_inds]]) > 0:
                self.cluster_losses[cluster] = np.mean(cluster_example_losses[self.has_loss[cluster_inds]])  # set the loss of this cluster of the mean if the losses exist
            else:
                self.cluster_losses[cluster] = 0

    def gen_batch(self):
        """
        Sample a batch by first sampling a seed cluster proportionally to
        the mean loss of the clusters, then finding nearest neighbor
        "impostor" clusters, then sampling d examples uniformly from each cluster.

        The generated batch will consist of m clusters each with d consecutive
        examples.
        """

        # Sample seed cluster proportionally to cluster losses if available
        if self.cluster_losses is not None:
            p = self.cluster_losses / np.sum(self.cluster_losses)
            seed_cluster = np.random.choice(self.num_classes * self.k, p=p)
        else:
            seed_cluster = np.random.choice(self.num_classes * self.k)

        # Get imposter clusters by ranking centroids by distance
        sq_dists = ((self.centroids[seed_cluster] - self.centroids) ** 2).sum(1).detach().cpu().numpy()

        # Assure only clusters of different class from seed are chosen
        # added the int condition as it was returning float and picking up seed_cluster
        sq_dists[int(self.get_class_ind(seed_cluster)) == self.cluster_classes] = np.inf

        # Get top m-1 (closest) impostor clusters and add seed cluster too
        clusters = np.argpartition(sq_dists, self.m - 1)[:self.m - 1]
        clusters = np.concatenate([[seed_cluster], clusters])

        # Sample d examples uniformly from m clusters
        batch_indexes = np.empty([self.m * self.d], int)
        for i, c in enumerate(clusters):
            x = np.random.choice(self.cluster_assignments[c], self.d, replace=True)  # if clusters have less than d samples we need to allow repick
            # x = np.random.choice(self.cluster_assignments[c], self.d, replace=False)
            start = i * self.d
            stop = start + self.d
            batch_indexes[start:stop] = x

        # Translate class indexes to index for classes within the batch
        # ie. [y1, y7, y56, y21, y89, y21,...] > [0, 1, 2, 3, 4, 3, ...]
        class_inds = self.cluster_classes[clusters]
        batch_class_inds = []
        inds_map = {}
        class_count = 0
        for c in class_inds:
            if c not in inds_map:
                inds_map[c] = class_count
                class_count += 1
            batch_class_inds.append(inds_map[c])

        return batch_indexes, np.repeat(batch_class_inds, self.d)

    def get_cluster_ind(self, c, i):
        """
        Given a class index and a cluster index within the class
        return the global cluster index
        """
        return c * self.k + i

    def get_class_ind(self, c):
        """
        Given a cluster index return the class index.
        """
        return c / self.k

    def predict(self, rep_data):
        """
        Predict the clusters that rep_data belongs to based on the clusters current positions
        :param rep_data:
        :return:
        """
        # sc = self.centroids - np.expand_dims(rep_data, 1)
        # sc = sc * sc
        # sc = sc.sum(2)

        sc = self.magnet_dist(self.centroids, torch.from_numpy(rep_data).float().cuda()).detach().cpu().numpy()

        preds = np.argmin(sc, 1)  # calc closest clusters

        # at this point the preds are the cluster indexs, so need to get classes
        for p in range(len(preds)):
            preds[p] = self.cluster_classes[preds[p]]  # first change to cluster index
            preds[p] = self.unique_classes[preds[p]]  # then convert into the actual class label
        return preds

    def calc_accuracy(self, rep_data, y):
        """
        Calculate the accuracy of reps based on the current clusters
        :param rep_data:
        :param y:
        :return:
        """
        preds = self.predict(rep_data)
        correct = preds == y
        acc = correct.astype(float).mean()
        return acc

    def dml_loss(self, r, classes, clusters, cluster_classes, n_clusters, alpha=1.0):
        """Compute magnet loss.

        Given a tensor of features `r`, the assigned class for each example,
        the assigned cluster for each example, the assigned class for each
        cluster, the total number of clusters, and separation hyperparameter,
        compute the magnet loss according to equation (4) in
        http://arxiv.org/pdf/1511.05939v2.pdf.

        Note that cluster and class indexes should be sequential startined at 0.

        Args:
            r: A batch of features.
            classes: Class labels for each example.
            clusters: Cluster labels for each example.
            cluster_classes: Class label for each cluster.
            n_clusters: Total number of clusters.
            alpha: The cluster separation gap hyperparameter.

        Returns:
            total_loss: The total magnet loss for the batch.
            losses: The loss for each example in the batch.
            acc: The predictive accuracy of the batch
        """
        # Helper to compute boolean mask for distance comparisons
        def comparison_mask(a_labels, b_labels):
            return torch.eq(a_labels.unsqueeze(1),
                            b_labels.unsqueeze(0))

        # Take cluster means within the batch
        # tf.dynamic_partition used clusters which is just an array we create in minibatch_magnet_loss, so can just reshape
        cluster_examples = r.view(n_clusters, int(float(r.shape[0])/float(n_clusters)), r.shape[1])
        cluster_means = cluster_examples.mean(1)

        # Compute squared distance of each example to each cluster centroid (euclid without the root)
        sample_costs = self.euclidean_distance(self.centroids, r.unsqueeze(1).cuda())

        # Select distances of examples to the centroids of same class... infact the closest (min cost)
        intra_cluster_costs = torch.ones_like(sample_costs)*(sample_costs.max()*1.5)
        for i, c in enumerate(classes.cpu().numpy()):
            # intra_cluster_costs[i, c] = sample_costs[i, c]
            inds = self.cluster_classes == c
            inds = inds.astype(int)
            intra_cluster_costs[i, inds.nonzero()] = sample_costs[i, inds.nonzero()]
        # intra_cluster_mask = comparison_mask(clusters, torch.arange(n_clusters)).float()
        # intra_cluster_costs = (intra_cluster_mask.cuda() * sample_costs).sum(1)
        intra_cluster_costs, _ = intra_cluster_costs.min(1)

        # Compute variance of intra-cluster distances
        N = r.shape[0]
        variance = intra_cluster_costs.sum() / float((N - 1))
        var_normalizer = -1 / (2 * variance**2)

        # Compute numerator
        numerator = torch.exp(var_normalizer * intra_cluster_costs - alpha)

        # Compute denominator
        # diff_class_mask = tf.to_float(tf.logical_not(comparison_mask(classes, cluster_classes)))
        diff_class_mask = (~comparison_mask(classes, cluster_classes)).float()
        denom_sample_costs = torch.exp(var_normalizer * sample_costs)
        denominator = (diff_class_mask.cuda() * denom_sample_costs).sum(1)

        # Compute example losses and total loss
        epsilon = 1e-8
        losses = F.relu(-torch.log(numerator / (denominator + epsilon) + epsilon))
        total_loss = losses.mean()

        _, preds = sample_costs.min(1)
        acc = torch.eq(classes, preds).float().mean()

        return total_loss, losses, acc


    def minibatch_dml_loss(self, r, classes, m, d, alpha=1.0):
        """Compute minibatch magnet loss.

        Given a batch of features `r` consisting of `m` clusters each with `d`
        consecutive examples, the corresponding class labels for each example
        and a cluster separation gap `alpha`, compute the total magnet loss and
        the per example losses. This is a thin wrapper around `magnet_loss`
        that assumes this particular batch structure to implement minibatch
        magnet loss according to equation (5) in
        http://arxiv.org/pdf/1511.05939v2.pdf. The correct stochastic approximation
        should also follow the cluster sampling procedure presented in section
        3.2 of the paper.

        Args:
            r: A batch of features.
            classes: Class labels for each example.
            m: The number of clusters in the batch.
            d: The number of examples in each cluster.
            alpha: The cluster separation gap hyperparameter.

        Returns:
            total_loss: The total magnet loss for the batch.
            losses: The loss for each example in the batch.
        """
        # clusters = np.repeat(np.arange(m, dtype=np.int32), d)
        clusters = torch.arange(m).unsqueeze(1).repeat(1, d).view(m*d)
        cluster_classes = classes.view(m, d)[:, 0]

        return self.dml_loss(r, classes, clusters, cluster_classes, m, alpha)

    @staticmethod
    def magnet_dist(means, reps):

        sample_costs = means - reps.unsqueeze(1)
        sample_costs = sample_costs * sample_costs
        sample_costs = sample_costs.sum(2)
        return sample_costs

    @staticmethod
    def euclidean_distance(x, y):
        return torch.sum((x - y)**2, dim=2)