from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import update_session_auth_hash
from .forms import UserProfileForm

@login_required
def profile_view(request):
    user = request.user
    
    if request.method == 'POST':
        if 'profile_submit' in request.POST:
            profile_form = UserProfileForm(request.POST, request.FILES, instance=user, prefix='profile')
            password_form = PasswordChangeForm(user, prefix='password')
            if profile_form.is_valid():
                user_obj = profile_form.save()
                
                # Sync with allauth if email changed
                if 'email' in profile_form.changed_data:
                    from allauth.account.models import EmailAddress
                    if EmailAddress.objects.filter(user=user_obj).exists():
                        EmailAddress.objects.filter(user=user_obj).update(email=user_obj.email)
                    else:
                        EmailAddress.objects.create(user=user_obj, email=user_obj.email, primary=True, verified=False)
                        
                messages.success(request, 'Your profile was successfully updated!')
                return redirect('users:profile')
            else:
                messages.error(request, 'Please correct the errors in the profile form.')
        elif 'password_submit' in request.POST:
            profile_form = UserProfileForm(instance=user, prefix='profile')
            password_form = PasswordChangeForm(user, request.POST, prefix='password')
            if password_form.is_valid():
                password_form.save()
                update_session_auth_hash(request, password_form.user)
                messages.success(request, 'Your password was successfully updated!')
                return redirect('users:profile')
            else:
                messages.error(request, 'Please correct the errors in the password form.')
        else:
            profile_form = UserProfileForm(instance=user, prefix='profile')
            password_form = PasswordChangeForm(user, prefix='password')
    else:
        profile_form = UserProfileForm(instance=user, prefix='profile')
        password_form = PasswordChangeForm(user, prefix='password')
        
    return render(request, 'users/profile.html', {
        'user': user,
        'profile_form': profile_form,
        'password_form': password_form,
    })
