from django.contrib import messages
from django.urls import reverse
from django.shortcuts import redirect
from . import auth


class object_access:

    # Initialize with an 'instance'= Project, Job, Data, or Analysis
    # redirects to self.instance.url() if no redirect_url is given
    def __init__(self, instance, owner_only=False, redirect_url=None):
        self.instance = instance
        self.owner_only = owner_only
        self.redirect_url = redirect_url

    def __call__(self, function, *args, **kwargs):
        """
           Decorator used to test if a user has rights to access an instance
       """

        def wrap(request, *args, **kwargs):
            id, user = kwargs['id'], request.user
            try:
                # Catch failure if instance doesnt have url() method
                # redirect to project_list when it fails
                self.redirect_url = self.redirect_url or self.instance.url()
            except:
                self.redirect_url = self.redirect_url or reverse("project_list")

            query = self.instance.objects.filter(pk=id).first()
            project, instance, allow_access = auth.check_obj_access(user, query,
                                                           owner_only=self.owner_only)

            # Everything has a project associated with it
            if not project:
                messages.error(request, f"Project with id={id} does not exist.")
                return redirect(self.redirect_url)

            if allow_access:
                return function(request, *args, **kwargs)
            else:
                #TODO: the redirection still needs some work
                messages.error(request, f"Access/modification to {self.instance.__name__}={id} denied.")
                return redirect(self.redirect_url)

        # wrap inherits attrs of the wrapped function
        wrap.__doc__ = function.__doc__
        wrap.__name__ = function.__name__
        return wrap


