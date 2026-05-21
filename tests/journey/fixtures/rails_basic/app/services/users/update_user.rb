module Users
  class UpdateUser
    def initialize(user, params)
      @user = user
      @params = params
    end

    def call
      @user.update!(@params)
    rescue ActiveRecord::RecordInvalid => e
      Result.failure(e.message)
    end
  end
end
